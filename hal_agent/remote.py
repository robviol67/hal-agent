"""
Control-plane remoto (Fase 2): l'agente scarica gli Scout dal SaaS e invia lo stato.

Modello server-authoritative:
  GET  {server_url}{config_path}  -> { "agents": [...], "days_limit": N, ... }
  POST {server_url}{status_path}  <- { "status": "testo breve" }

Gli Scout definiti sul SaaS hanno la precedenza sul config.json locale. L'ultima
config valida viene messa in cache su disco per funzionare anche offline.
"""
import json
import logging

import httpx

from . import config as cfg

log = logging.getLogger("hal_agent.remote")

REMOTE_CACHE = cfg.CONFIG_DIR / "remote_config.json"


def _headers(conf: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if conf.get("token"):
        h["Authorization"] = f"Bearer {conf['token']}"
    return h


def fetch_config(conf: dict):
    """Scarica la config (Scout) dal SaaS. Ritorna il dict o None se non raggiungibile."""
    if not conf.get("token"):
        return None
    server = conf["server_url"].rstrip("/")
    path = conf.get("config_path", "/api/agent/config")
    try:
        r = httpx.get(server + path, headers=_headers(conf), timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get("agents"), list):
            _cache_config(data)
            return data
        log.debug("config remota in formato inatteso")
    except Exception as e:
        log.debug("fetch_config fallito: %s", e)
    return None


def probe(conf: dict):
    """
    Prova l'accoppiamento col SaaS: chiama config_path e dice com'è andata.
    Ritorna (ok: bool, messaggio: str, n_scout: int). Usato dal Pannello.
    """
    if not (conf.get("server_url") or "").strip():
        return False, "Manca l'indirizzo del server.", 0
    if not (conf.get("token") or "").strip():
        return False, "Manca il token di accoppiamento.", 0
    server = conf["server_url"].rstrip("/")
    path = conf.get("config_path", "/api/agent/config")
    try:
        r = httpx.get(server + path, headers=_headers(conf), timeout=20)
    except Exception as e:
        return False, "Server non raggiungibile: %s" % str(e)[:160], 0
    if r.status_code in (401, 403):
        return False, "Token rifiutato dal server (HTTP %d)." % r.status_code, 0
    if r.status_code >= 400:
        return False, "HTTP %d — %s" % (r.status_code, r.text[:160]), 0
    try:
        data = r.json()
        agents = data.get("agents")
    except Exception:
        return False, "Risposta non in formato JSON.", 0
    if not isinstance(agents, list):
        return False, "Il server non ha restituito l'elenco degli Scout.", 0
    _cache_config(data)
    return True, "Collegato: %d Scout assegnati a questo agente." % len(agents), len(agents)


def _cache_config(data: dict) -> None:
    try:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        REMOTE_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug("cache config non salvata: %s", e)


def load_cached_config():
    """Ultima config remota valida salvata su disco, o None."""
    if not REMOTE_CACHE.exists():
        return None
    try:
        data = json.loads(REMOTE_CACHE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("agents"), list):
            return data
    except Exception:
        pass
    return None


def send_status(conf: dict, msg: str) -> None:
    """Invia una riga di stato al SaaS (best-effort, non blocca il giro se fallisce)."""
    if not conf.get("token"):
        return
    server = conf["server_url"].rstrip("/")
    path = conf.get("status_path", "/api/agent/status")
    try:
        httpx.post(server + path, headers=_headers(conf),
                   json={"status": (msg or "")[:255]}, timeout=10)
    except Exception as e:
        log.debug("send_status fallito: %s", e)


def report_bridge(conf: dict, enabled: bool, ok: bool, endpoint: str, detail: str) -> None:
    """
    Riporta lo stato del ponte LLM (raggiungibilità) al SaaS. NON invia 'status'
    così non azzera la riga di stato della raccolta.
    """
    if not conf.get("token"):
        return
    server = conf["server_url"].rstrip("/")
    path = conf.get("status_path", "/api/agent/status")
    try:
        httpx.post(server + path, headers=_headers(conf),
                   json={"bridge": {
                       "enabled": bool(enabled),
                       "ok": bool(ok),
                       "endpoint": (endpoint or "")[:255],
                       "detail": (detail or "")[:200],
                   }}, timeout=10)
    except Exception as e:
        log.debug("report_bridge fallito: %s", e)
