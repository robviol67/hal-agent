"""Invio degli item raccolti al HAL-SaaS via API, con dedup per URL."""
import json
import logging

import httpx

from . import config as cfg

log = logging.getLogger("hal_agent.sender")


def filter_new(items: list, state: dict) -> list:
    """Tiene solo gli item la cui URL non è già stata inviata."""
    seen = set(state.get("seen_urls", []))
    out = []
    for it in items:
        url = it.get("url") or ""
        if url and url in seen:
            continue
        out.append(it)
    return out


def send(items: list, conf: dict, dry_run: bool = False) -> dict:
    """
    Invia gli item al SaaS. Contratto:
      POST {server_url}{ingest_path}
      Authorization: Bearer <token>
      body: { "items": [ {title, excerpt, url, source, published, channel, author, agent}, ... ] }
    In dry-run stampa il payload senza inviare.
    """
    payload = {"items": items, "agent_version": _version()}
    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return {"ok": True, "sent": len(items), "dry_run": True}

    url = conf["server_url"].rstrip("/") + conf.get("ingest_path", "/api/agent/ingest")
    headers = {"Content-Type": "application/json"}
    if conf.get("token"):
        headers["Authorization"] = f"Bearer {conf['token']}"
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        log.info("Inviati %d item → %s (%s)", len(items), url, r.status_code)
        return {"ok": True, "sent": len(items), "status": r.status_code}
    except Exception as e:
        log.error("Invio fallito verso %s: %s", url, e)
        return {"ok": False, "error": str(e), "sent": 0}


def mark_sent(items: list, state: dict) -> None:
    seen = state.setdefault("seen_urls", [])
    for it in items:
        u = it.get("url")
        if u:
            seen.append(u)


def _version() -> str:
    from . import __version__
    return __version__
