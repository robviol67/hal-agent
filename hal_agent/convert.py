"""
Conversioni su richiesta (v0.7.0): HAL chiede, l'agente converte.

Il server non sa ricavare il testo dei PDF (né degli RTF); l'agente sì (docmd.py,
PDFKit di macOS). Quando a HAL serve il Markdown di un documento GIÀ
nell'Archivio — Callimaco che deve leggere un PDF, «↻ Riconverti con l'agente» —
mette una richiesta in coda. Qui l'agente:

  GET  {server}/api/agent/convert?v=<versione>  -> {"jobs": [{id, title, ext, file}, …]}
  GET  {server}{file}                            -> i byte del documento
  POST {server}/api/agent/convert  {"id", "md"} oppure {"id", "error"}

Il file si scarica in una cartella temporanea e si cancella subito dopo: sul Mac
non resta niente. Il Markdown non si scrive mai su disco.
"""
import logging
import os
import shutil
import tempfile

import httpx

from . import config as cfg
from . import docmd

log = logging.getLogger("hal_agent.convert")

_MAX_BYTES = 60 * 1024 * 1024


def _version() -> str:
    from . import __version__
    return __version__


def run_once(conf: dict = None, on_progress=None) -> dict:
    """Un giro: prende le richieste in coda e le evade. Ritorna un riepilogo."""
    conf = conf or cfg.load_config()
    c = conf.get("convert", {}) or {}
    res = {"jobs": 0, "done": 0, "failed": 0, "ok": True}

    def prog(m):
        if on_progress:
            on_progress(m)

    token = conf.get("token") or ""
    base = (conf.get("server_url") or "").rstrip("/")
    if not token or not base:
        res.update(ok=False, error_msg="collegamento non configurato")
        return res
    url = base + (c.get("poll_path") or "/api/agent/convert")
    headers = {"Authorization": f"Bearer {token}"}

    try:
        r = httpx.get(url, params={"v": _version()}, headers=headers, timeout=30)
        r.raise_for_status()
        jobs = (r.json() or {}).get("jobs") or []
    except Exception as e:
        log.debug("coda conversioni non raggiungibile: %s", e)
        res.update(ok=False, error_msg=str(e)[:200])
        return res

    for job in jobs:
        res["jobs"] += 1
        jid = job.get("id")
        title = (job.get("title") or "documento")[:80]
        ext = (job.get("ext") or "").lower().strip(".")
        prog(f"Conversioni: «{title}»…")
        md, why = "", ""
        tmpdir = tempfile.mkdtemp(prefix="hal-convert-")
        try:
            path = os.path.join(tmpdir, f"documento.{ext or 'bin'}")
            size = 0
            with httpx.stream("GET", base + (job.get("file") or ""), headers=headers, timeout=180) as resp:
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > _MAX_BYTES:
                            raise RuntimeError("file troppo grande")
                        fh.write(chunk)
            md, why = docmd.convert_to_markdown(path)
            # il titolo vero lo sa HAL: si sostituisce al «# documento» del file temporaneo
            if md.startswith("# documento\n"):
                md = f"# {title}\n" + md[len("# documento\n"):]
        except Exception as e:
            why = f"download o conversione non riusciti: {str(e)[:160]}"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        payload = {"id": jid, "md": md} if md else {"id": jid, "error": why or "nessun testo estratto"}
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=120)
            r.raise_for_status()
            ok = (r.json() or {}).get("status") == "done"
        except Exception as e:
            log.error("risultato della conversione non consegnato (%s): %s", title, e)
            ok = False
        if ok:
            res["done"] += 1
            prog(f"Conversioni: «{title}» pronto ({len(md)} caratteri)")
        else:
            res["failed"] += 1
            prog(f"Conversioni: «{title}» non convertito — {why or 'errore di rete'}")
    return res
