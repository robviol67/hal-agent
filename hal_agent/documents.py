"""Capability «Documenti»: osserva una cartella locale e versa i materiali nuovi
nell'Archivio documenti di HAL (POST /api/agent/doc_upload).

È la gemella di library.py, ma punta a una porta diversa:
  - library.py  → Frontiera dei LIBRI (opere da catalogare in Dogana);
  - documents.py → Archivio DOCUMENTI, coda «Da classificare» (materiali di
    studio e di campagna: dispense, slide, appunti, brief).

Flusso per ogni file nuovo trovato nella cartella osservata:
  1. hash del file (con cache size+mtime, come per la Libreria);
  2. `check` al server: si scarta subito ciò che l'Archivio ha già;
  3. conversione in Markdown IN MEMORIA (docmd.py) — il .md non viene mai
     scritto su disco, viaggia come campo del multipart;
  4. upload di file + `md` insieme, e aggiornamento dello stato locale.

Differenza importante rispetto alla Libreria: qui gli originali NON vengono
rinominati né spostati. Sono i file di lavoro dell'utente, dentro le sue
cartelle: cambiarli sotto i piedi sarebbe invasivo. Il dedup si regge quindi
tutto sugli hash (locali + server), non sullo spostamento in «inviati/».
Chi vuole il comportamento della Libreria può accendere documents.move_sent.

Il pdf si carica SENZA `md`: la sua estrazione la fa il server (o il browser
dell'utente, che risponde con needs_browser).
"""
import hashlib
import logging
import os
import shutil

import httpx

from . import config as cfg
from . import docmd

log = logging.getLogger("hal_agent.documents")

_SKIP_PREFIX = (".", "~$")
_SKIP_SUFFIX = (".crdownload", ".part", ".tmp", ".download")
_CHECK_CHUNK = 200


def _iter_files(folder: str, exts: set, exclude_dirs: set):
    """File supportati nella cartella (ricorsiva), saltando nascosti/temporanei
    e le sotto-cartelle in exclude_dirs (es. «inviati»).

    Nota macOS: un .key (Keynote) può essere un PACCHETTO, cioè una cartella.
    Le cartelle con estensione gestita vengono potate: non ha senso scendere
    dentro un pacchetto e caricarne le viscere."""
    ex = {d.lower() for d in exclude_dirs}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and d.lower() not in ex
                   and os.path.splitext(d)[1].lower().lstrip(".") not in exts]
        for name in files:
            if name.startswith(_SKIP_PREFIX) or name.lower().endswith(_SKIP_SUFFIX):
                continue
            ext = os.path.splitext(name)[1].lower().lstrip(".")
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
    """Hash già presenti nell'Archivio (modalità CHECK: body JSON {"check": [...]})."""
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


def _upload_one(url: str, headers: dict, path: str, filename: str, md: str) -> tuple:
    """Carica un documento (multipart: file + filename + md). Ritorna
    (status, needs_browser): status in stored | duplicate | error."""
    data = {"filename": filename}
    if md:
        data["md"] = md
    try:
        with open(path, "rb") as fh:
            files = {"file": (filename, fh, "application/octet-stream")}
            r = httpx.post(url, files=files, data=data, headers=headers, timeout=180)
        r.raise_for_status()
        body = r.json() or {}
        return body.get("status", "stored"), bool(body.get("needs_browser"))
    except Exception as e:
        log.error("upload documento fallito per %s: %s", filename, e)
        return "error", False


def _dedupe_target(target: str, seed: str) -> str:
    """Se il percorso esiste già, inserisce un breve suffisso stabile prima dell'estensione."""
    if not os.path.exists(target):
        return target
    root, ext = os.path.splitext(target)
    suf = hashlib.sha1(seed.encode("utf-8", "ignore")).hexdigest()[:6]
    cand = f"{root}-{suf}{ext}"
    return cand if not os.path.exists(cand) else f"{root}-{suf}-{os.getpid()}{ext}"


def _move_to_sent(path: str, sent_dir: str) -> str:
    os.makedirs(sent_dir, exist_ok=True)
    target = os.path.join(sent_dir, os.path.basename(path))
    target = _dedupe_target(target, path)
    shutil.move(path, target)
    return target


def scan_once(conf: dict = None, on_progress=None, dry_run: bool = False) -> dict:
    """Un giro: trova i documenti nuovi, li converte in Markdown (in memoria) e li
    carica nell'Archivio documenti. Ritorna un riepilogo."""
    conf = conf or cfg.load_config()
    doc = conf.get("documents", {}) or {}

    def prog(m):
        if on_progress:
            on_progress(m)

    result = {"scanned": 0, "new": 0, "known": 0, "uploaded": 0, "duplicate": 0, "error": 0,
              "converted": 0, "no_md": 0, "needs_browser": 0, "moved": 0,
              "skipped_big": 0, "folder": doc.get("folder", ""), "ok": True}

    folder = doc.get("folder") or ""
    if not folder or not os.path.isdir(folder):
        result.update(ok=False, error_msg="cartella inesistente")
        prog(f"Documenti: cartella non trovata ({folder or '—'})")
        return result

    token = conf.get("token") or ""
    if not token and not dry_run:
        result.update(ok=False, error_msg="token mancante")
        prog("Documenti: token mancante (configura il collegamento)")
        return result

    exts = set(doc.get("exts") or [])
    max_bytes = int(doc.get("max_mb", 48)) * 1024 * 1024
    do_convert = bool(doc.get("convert_markdown", True))
    do_move = bool(doc.get("move_sent", False))
    sent_subdir = str(doc.get("sent_subdir") or "inviati")
    sent_dir = os.path.join(folder, sent_subdir)
    url = conf.get("server_url", "").rstrip("/") + doc.get("upload_path", "/api/agent/doc_upload")
    headers = {"Authorization": f"Bearer {token}"}

    # In prova a secco non si tocca nulla: né rete, né state.json, né config.
    state = {} if dry_run else cfg.load_state()
    docstate = state.setdefault("documents", {}) if not dry_run else {}
    cache = docstate.setdefault("cache", {}) if not dry_run else {}
    sent = set(docstate.setdefault("sent_hashes", [])) if not dry_run else set()

    prog(f"Documenti: scansione di {folder}…")

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
        entries.append((path, h, os.path.splitext(path)[1].lower().lstrip(".")))

    for gone in [p for p in cache if p not in seen_paths]:
        cache.pop(gone, None)

    # 2) quali hash sono già noti al server (per non ricaricarli)
    candidates = [h for (_p, h, _e) in entries if h not in sent]
    known_server = set()
    if candidates and not dry_run:
        known_server = _server_known(url, headers, list(dict.fromkeys(candidates)))
        if known_server:
            prog(f"Documenti: {len(known_server)} già nell'Archivio")

    todo = [(p, h, e) for (p, h, e) in entries if h not in sent and h not in known_server]
    result["new"] = len(todo)
    result["known"] = len(entries) - len(todo)      # già in Archivio: si saltano

    # 3) DRY-RUN: converte e mostra cosa verrebbe spedito, senza rete e senza stato
    if dry_run:
        for path, _h, ext in todo:
            if do_convert:
                md, why = docmd.convert_to_markdown(path)
            else:
                md, why = "", "conversione disattivata"
            if md:
                result["converted"] += 1
                head = next((ln for ln in md.split("\n") if ln.strip()), "")
                print(f"{os.path.basename(path)}  →  md {len(md)} caratteri  |  {head[:60]}")
            else:
                result["no_md"] += 1
                print(f"{os.path.basename(path)}  →  senza md ({why})")
        prog(f"Documenti (dry-run): {result['new']} nuovi su {result['scanned']} file, "
             f"{result['converted']} convertiti")
        return result

    # 4) elabora ogni documento nuovo: converti in memoria → carica → (sposta)
    total = len(todo)
    for i, (path, h, ext) in enumerate(todo):
        try:
            filename = os.path.basename(path)
            md, why = ("", "conversione disattivata")
            if do_convert:
                md, why = docmd.convert_to_markdown(path)
            if md:
                result["converted"] += 1
            else:
                result["no_md"] += 1
                log.debug("niente markdown per %s: %s", filename, why)

            prog(f"Documenti: invio {i + 1}/{total} — {filename}"
                 + (f" (md {len(md)} caratteri)" if md else ""))
            status, needs_browser = _upload_one(url, headers, path, filename, md)
            if status == "stored":
                result["uploaded"] += 1
            elif status == "duplicate":
                result["duplicate"] += 1
            else:
                result["error"] += 1
                continue          # errore: si ritenta al prossimo giro
            if needs_browser:
                result["needs_browser"] += 1
            sent.add(h)

            if do_move:
                _move_to_sent(path, sent_dir)
                result["moved"] += 1
        except OSError as e:
            log.error("Documenti: errore su %s: %s", path, e)
            result["error"] += 1

    docstate["sent_hashes"] = list(sent)[-20000:]
    cfg.save_state(state)

    msg = (f"Documenti: {result['uploaded']} inviati, "
           f"{result['known'] + result['duplicate']} già presenti, "
           f"{result['converted']} convertiti in Markdown")
    if result["needs_browser"]:
        msg += f", {result['needs_browser']} da indicizzare nel browser"
    if result["error"]:
        msg += f", {result['error']} errori"
    prog(msg)
    log.info("Documenti: scan=%d nuovi=%d inviati=%d dup=%d md=%d err=%d",
             result["scanned"], result["new"], result["uploaded"], result["duplicate"],
             result["converted"], result["error"])
    return result
