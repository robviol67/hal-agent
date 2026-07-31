"""Capability «Libreria»: osserva una cartella locale e versa i libri nuovi nella
Frontiera di HAL (POST /api/agent/upload).

Flusso per ogni libro nuovo trovato nella cartella osservata:
  1. legge i metadati locali (best-effort) e RINOMINA il file in
     «Titolo-autore-genere-anno.ext» (config library.rename);
  2. lo CARICA nella Frontiera del sito (dedup per hash);
  3. lo SPOSTA nella sotto-cartella «inviati/» (config library.move_sent), che è
     esclusa dalle scansioni successive → niente re-invii.

Filosofia (speculare al drag&drop sul sito): l'agente versa in AUTOMATICO, quindi
i suoi libri NON vengono catalogati subito — si fermano nella Frontiera, inerti,
finché Rob non li «presenta all'identificazione». La catalogazione vera (metadati
OpenLibrary/Google Books) la fa la Dogana lato server: la rinomina locale serve
solo a tenere ordinata la cartella e la lista Frontiera.

Dedup a due livelli, così ricaricare la cartella è sempre sicuro:
  - locale:  state.json → library.sent_hashes;
  - server:  /api/agent/upload deduplica per hash, e la modalità `check` dice in
             anticipo cosa esiste già.
"""
import hashlib
import logging
import os
import shutil

import httpx

from . import bookmeta_local as meta
from . import config as cfg

log = logging.getLogger("hal_agent.library")

_SKIP_PREFIX = (".", "~$")
_SKIP_SUFFIX = (".crdownload", ".part", ".tmp", ".download")
_CHECK_CHUNK = 200


def _iter_files(folder: str, exts: set, exclude_dirs: set):
    """File supportati nella cartella (ricorsiva), saltando nascosti/temporanei
    e le sotto-cartelle in exclude_dirs (es. «inviati»)."""
    ex = {d.lower() for d in exclude_dirs}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ex]
        for name in files:
            if name.startswith(_SKIP_PREFIX) or name.lower().endswith(_SKIP_SUFFIX):
                continue
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext == "doc":
                ext = "docx"
            if ext in exts:
                yield os.path.join(root, name)


def _hash_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cached_hash(path: str, cache: dict) -> tuple:
    st = os.stat(path)
    size, mtime = st.st_size, st.st_mtime
    ent = cache.get(path)
    if ent and int(ent.get("size", -1)) == size and abs(float(ent.get("mtime", -1)) - mtime) < 1e-6:
        return ent["hash"], size
    h = _hash_file(path)
    cache[path] = {"size": size, "mtime": mtime, "hash": h}
    return h, size


def _server_known(url: str, headers: dict, hashes: list) -> set:
    known = set()
    for i in range(0, len(hashes), _CHECK_CHUNK):
        chunk = hashes[i:i + _CHECK_CHUNK]
        try:
            r = httpx.post(url, json={"check": chunk}, headers=headers, timeout=30)
            r.raise_for_status()
            known.update(r.json().get("known", []) or [])
        except Exception as e:
            log.debug("check fallito (%d hash): %s", len(chunk), e)
    return known


def _upload_one(url: str, headers: dict, path: str, filename: str) -> str:
    try:
        with open(path, "rb") as fh:
            files = {"file": (filename, fh, "application/octet-stream")}
            r = httpx.post(url, files=files, data={"filename": filename}, headers=headers, timeout=180)
        r.raise_for_status()
        return (r.json() or {}).get("status", "stored")
    except Exception as e:
        log.error("upload fallito per %s: %s", filename, e)
        return "error"


def _dedupe_target(target: str, seed: str) -> str:
    """Se il percorso esiste già, inserisce un breve suffisso stabile prima dell'estensione."""
    if not os.path.exists(target):
        return target
    root, ext = os.path.splitext(target)
    suf = hashlib.sha1(seed.encode("utf-8", "ignore")).hexdigest()[:6]
    cand = f"{root}-{suf}{ext}"
    return cand if not os.path.exists(cand) else f"{root}-{suf}-{os.getpid()}{ext}"


def _rename_in_place(path: str, newname: str) -> str:
    """Rinomina il file nella stessa cartella. Ritorna il nuovo percorso (o quello
    invariato se il nome coincide già)."""
    d = os.path.dirname(path)
    target = os.path.join(d, newname)
    if os.path.abspath(target) == os.path.abspath(path):
        return path
    target = _dedupe_target(target, os.path.basename(path))
    os.rename(path, target)
    return target


def _move_to_sent(path: str, sent_dir: str) -> str:
    os.makedirs(sent_dir, exist_ok=True)
    target = os.path.join(sent_dir, os.path.basename(path))
    target = _dedupe_target(target, path)
    shutil.move(path, target)
    return target


def _final_name(path: str, ext: str, do_rename: bool) -> str:
    """Nome con cui spedire/archiviare il file (rinominato o quello originale)."""
    if not do_rename:
        return os.path.basename(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    return meta.build_name(meta.extract(path, ext), ext, stem)


def scan_once(conf: dict = None, on_progress=None, dry_run: bool = False) -> dict:
    """Un giro: trova i libri nuovi, li rinomina, li carica nella Frontiera e li
    sposta in inviati/. Ritorna un riepilogo."""
    conf = conf or cfg.load_config()
    lib = conf.get("library", {}) or {}

    def prog(m):
        if on_progress:
            on_progress(m)

    result = {"scanned": 0, "new": 0, "uploaded": 0, "duplicate": 0, "error": 0,
              "moved": 0, "renamed": 0, "skipped_big": 0, "folder": lib.get("folder", ""), "ok": True}

    folder = lib.get("folder") or ""
    if not folder or not os.path.isdir(folder):
        result.update(ok=False, error_msg="cartella inesistente")
        prog(f"Libreria: cartella non trovata ({folder or '—'})")
        return result

    token = conf.get("token") or ""
    if not token and not dry_run:
        result.update(ok=False, error_msg="token mancante")
        prog("Libreria: token mancante (configura il collegamento)")
        return result

    exts = set(lib.get("exts") or [])
    max_bytes = int(lib.get("max_mb", 48)) * 1024 * 1024
    do_rename = bool(lib.get("rename", True))
    do_move = bool(lib.get("move_sent", True))
    sent_subdir = str(lib.get("sent_subdir") or "inviati")
    sent_dir = os.path.join(folder, sent_subdir)
    url = conf["server_url"].rstrip("/") + lib.get("upload_path", "/api/agent/upload")
    headers = {"Authorization": f"Bearer {token}"}

    state = cfg.load_state()
    libstate = state.setdefault("library", {})
    cache = libstate.setdefault("cache", {})
    sent = set(libstate.setdefault("sent_hashes", []))

    prog(f"Libreria: scansione di {folder}…")

    # 1) file + hash (con cache), saltando i troppo grandi e la cartella inviati/
    entries = []       # (path, hash, ext)
    seen_paths = set()
    for path in _iter_files(folder, exts, {sent_subdir}):
        seen_paths.add(path)
        try:
            h, size = _cached_hash(path, cache)
        except OSError as e:
            log.debug("stat/hash fallito %s: %s", path, e)
            continue
        if size > max_bytes:
            result["skipped_big"] += 1
            continue
        result["scanned"] += 1
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext == "doc":
            ext = "docx"
        entries.append((path, h, ext))

    for gone in [p for p in cache if p not in seen_paths]:
        cache.pop(gone, None)

    # 2) quali hash sono già noti al server (per non ricaricarli)
    candidates = [h for (_p, h, _e) in entries if h not in sent]
    known_server = set()
    if candidates and not dry_run:
        known_server = _server_known(url, headers, list(dict.fromkeys(candidates)))
        if known_server:
            prog(f"Libreria: {len(known_server)} già noti al server")

    # 3) DRY-RUN: mostra i nomi proposti, non tocca nulla
    if dry_run:
        for path, h, ext in entries:
            if h in sent or h in known_server:
                continue
            print(f"{os.path.basename(path)}  →  {_final_name(path, ext, do_rename)}")
            result["new"] += 1
        libstate["sent_hashes"] = list(sent)
        cfg.save_state(state)
        prog(f"Libreria (dry-run): {result['new']} nuovi su {result['scanned']} file")
        return result

    # 4) elabora ogni file: rinomina → (carica se nuovo) → sposta in inviati/
    result["new"] = len([h for (_p, h, _e) in entries if h not in sent and h not in known_server])
    total = len(entries)
    for i, (path, h, ext) in enumerate(entries):
        try:
            already = h in sent or h in known_server   # già in HAL: solo riordino
            newname = _final_name(path, ext, do_rename)

            # rinomina in loco (prima della spedizione)
            cur = path
            if do_rename and newname != os.path.basename(path):
                cur = _rename_in_place(path, newname)
                result["renamed"] += 1

            if not already:
                prog(f"Libreria: invio {i + 1}/{total} — {newname}")
                status = _upload_one(url, headers, cur, newname)
                if status == "stored":
                    result["uploaded"] += 1
                elif status == "duplicate":
                    result["duplicate"] += 1
                else:
                    result["error"] += 1
                    continue          # errore: lascia il file dov'è, ritenta al prossimo giro
                sent.add(h)
            else:
                result["duplicate"] += 1

            # sposta in inviati/ (file ormai in HAL)
            if do_move:
                _move_to_sent(cur, sent_dir)
                result["moved"] += 1
        except OSError as e:
            log.error("Libreria: errore su %s: %s", path, e)
            result["error"] += 1

    libstate["sent_hashes"] = list(sent)[-20000:]
    cfg.save_state(state)

    prog(f"Libreria: {result['uploaded']} inviati, {result['duplicate']} già presenti, "
         f"{result['moved']} spostati" + (f", {result['error']} errori" if result["error"] else ""))
    log.info("Libreria: scan=%d nuovi=%d inviati=%d dup=%d moved=%d err=%d",
             result["scanned"], result["new"], result["uploaded"], result["duplicate"],
             result["moved"], result["error"])
    return result
