"""
Avviso di versione nuova (v0.9.5). La barra dei menu controlla le Release su GitHub
ogni 6 ore e scrive l'esito in ~/.hal-agent/aggiornamento.json; il pannello lo legge
e mostra «È uscita la vX». La notifica di macOS parte una volta sola per versione.
"""
import json
import logging
import os
import time

from . import __version__
from . import config as cfg

log = logging.getLogger("hal_agent.updates")

REPO = "robviol67/hal-agent"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
FILE = cfg.CONFIG_DIR / "aggiornamento.json"
EVERY = 6 * 3600


def parse_version(v: str):
    """'v0.2.1' / '0.2.1' → (0, 2, 1) per confronto; parti non numeriche = 0."""
    v = (v or "").lstrip("vV").strip()
    out = []
    for part in v.split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out) if out else (0,)


def read() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    try:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, FILE)
    except Exception as e:
        log.debug("aggiornamento.json non scritto: %s", e)


def newer() -> str:
    """Il tag della versione più nuova di questa, se c'è (dall'ultimo controllo), altrimenti ''."""
    d = read()
    latest = d.get("latest") or ""
    return latest if latest and parse_version(latest) > parse_version(__version__) else ""


def check(fetch, force: bool = False) -> str:
    """Controlla (al più ogni 6 ore). fetch() → tag o None. Ritorna il tag da NOTIFICARE
    (nuovo e non ancora notificato), altrimenti ''."""
    d = read()
    if not force and time.time() - d.get("checked", 0) < EVERY:
        return ""
    latest = fetch()
    d["checked"] = time.time()
    if latest:
        d["latest"] = latest
    out = ""
    if latest and parse_version(latest) > parse_version(__version__) and d.get("notified") != latest:
        d["notified"] = latest
        out = latest
    _write(d)
    return out
