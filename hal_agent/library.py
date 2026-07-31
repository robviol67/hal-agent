"""Capability «Libreria»: osserva una cartella locale e versa i libri nuovi nella
Frontiera di HAL (POST /api/agent/upload).

Filosofia (speculare al drag&drop sul sito): l'agente versa in AUTOMATICO e in
massa, quindi i suoi libri NON vengono catalogati subito — si fermano nella
Frontiera del sito, inerti, finché Rob non li «presenta all'identificazione». Qui
ci limitiamo a: trovare i file supportati, calcolarne l'hash (con cache su
dimensione+mtime per non rileggerli ogni giro), chiedere al server quali sono già
noti, e caricare solo i nuovi.

Dedup a due livelli, così ricaricare la cartella è sempre sicuro:
  - locale:  state.json → library.sent_hashes (già confermati dal server);
  - server:  /api/agent/upload deduplica per hash su catalogo/coda/frontiera, e la
             modalità `check` dice in anticipo cosa esiste già (utile se lo
             state.json locale viene azzerato: non ricarichiamo l'intera libreria).

L'agente NON tocca i file originali dell'utente: la cartella resta com'è.
"""
import hashlib
import logging
import os

import httpx

from . import config as cfg

log = logging.getLogger("hal_agent.library")

# file temporanei / parziali da ignorare
_SKIP_PREFIX = (".", "~$")
_SKIP_SUFFIX = (".crdownload", ".part", ".tmp", ".download")
_CHECK_CHUNK = 200          # quanti hash per chiamata `check`


def _iter_files(folder: str, exts: set):
    """Percorre la cartella (ricorsiva) e restituisce i path dei file supportati."""
    for root, dirs, files in os.walk(folder):
        # non scendere in cartelle nascoste (es. .git, .Trash)
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith(_SKIP_PREFIX) or name.lower().endswith(_SKIP_SUFFIX):
                continue
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext == "doc":
                ext = "docx"
            if ext in exts:
                yield os.path.join(root, name)


def _hash_file(path: str) -> str:
    """sha1 del file, in streaming (grandi PDF senza caricare tutto in RAM)."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cached_hash(path: str, cache: dict) -> tuple:
    """(hash, size) usando la cache su (size, mtime); ricalcola solo se il file è cambiato."""
    st = os.stat(path)
    size, mtime = st.st_size, st.st_mtime
    ent = cache.get(path)
    if ent and int(ent.get("size", -1)) == size and abs(float(ent.get("mtime", -1)) - mtime) < 1e-6:
        return ent["hash"], size
    h = _hash_file(path)
    cache[path] = {"size": size, "mtime": mtime, "hash": h}
    return h, size


def _server_known(url: str, headers: dict, hashes: list) -> set:
    """Chiede al server quali hash sono già noti (catalogo/coda/frontiera)."""
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
    """Carica un file nella Frontiera. Ritorna 'stored' | 'duplicate' | 'error'."""
    try:
        with open(path, "rb") as fh:
            files = {"file": (filename, fh, "application/octet-stream")}
            r = httpx.post(url, files=files, data={"filename": filename}, headers=headers, timeout=180)
        r.raise_for_status()
        return (r.json() or {}).get("status", "stored")
    except Exception as e:
        log.error("upload fallito per %s: %s", filename, e)
        return "error"


def scan_once(conf: dict = None, on_progress=None, dry_run: bool = False) -> dict:
    """
    Un giro completo: trova i file supportati, filtra i nuovi (locale + server),
    carica i nuovi nella Frontiera. Ritorna un riepilogo.
    """
    conf = conf or cfg.load_config()
    lib = conf.get("library", {}) or {}

    def prog(msg):
        if on_progress:
            on_progress(msg)

    result = {"scanned": 0, "new": 0, "uploaded": 0, "duplicate": 0,
              "error": 0, "skipped_big": 0, "folder": lib.get("folder", ""), "ok": True}

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
    url = conf["server_url"].rstrip("/") + lib.get("upload_path", "/api/agent/upload")
    headers = {"Authorization": f"Bearer {token}"}

    state = cfg.load_state()
    libstate = state.setdefault("library", {})
    cache = libstate.setdefault("cache", {})
    sent = set(libstate.setdefault("sent_hashes", []))

    prog(f"Libreria: scansione di {folder}…")

    # 1) trova i file + hash (con cache); scarta i troppo grandi
    by_hash = {}   # hash -> (path, filename)
    seen_paths = set()
    for path in _iter_files(folder, exts):
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
        by_hash.setdefault(h, (path, os.path.basename(path)))

    # potatura cache: dimentica i path spariti dalla cartella
    for gone in [p for p in cache if p not in seen_paths]:
        cache.pop(gone, None)

    # 2) candidati = hash non ancora confermati localmente
    candidates = [h for h in by_hash if h not in sent]

    # 3) filtro server: cosa è già noto (catalogo/coda/frontiera) → lo segno "sent" senza ricaricarlo
    if candidates and not dry_run:
        known = _server_known(url, headers, candidates)
        if known:
            sent.update(known)
            candidates = [h for h in candidates if h not in known]
            prog(f"Libreria: {len(known)} già noti al server (saltati)")

    result["new"] = len(candidates)

    if dry_run:
        for h in candidates:
            print(by_hash[h][1])
        prog(f"Libreria (dry-run): {len(candidates)} nuovi su {result['scanned']} file")
        libstate["sent_hashes"] = list(sent)
        cfg.save_state(state)
        return result

    # 4) carica i nuovi, uno per uno
    total = len(candidates)
    for i, h in enumerate(candidates):
        path, filename = by_hash[h]
        prog(f"Libreria: invio {i + 1}/{total} — {filename}")
        status = _upload_one(url, headers, path, filename)
        if status in ("stored", "duplicate"):
            sent.add(h)
            result["uploaded" if status == "stored" else "duplicate"] += 1
        else:
            result["error"] += 1

    libstate["sent_hashes"] = list(sent)[-20000:]   # tetto prudenziale
    cfg.save_state(state)

    prog(f"Libreria: {result['uploaded']} inviati, {result['duplicate']} già presenti"
         + (f", {result['error']} errori" if result["error"] else ""))
    log.info("Libreria: scan=%d nuovi=%d inviati=%d dup=%d err=%d",
             result["scanned"], result["new"], result["uploaded"], result["duplicate"], result["error"])
    return result
