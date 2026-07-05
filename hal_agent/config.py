"""Gestione configurazione dell'agente (file JSON nella home dell'utente)."""
import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".hal-agent"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"   # dedup: URL già inviate

DEFAULT_CONFIG = {
    "server_url": "http://localhost:8000",   # endpoint del HAL-SaaS
    "token": "",                              # token di pairing (Bearer)
    "ingest_path": "/api/agent/ingest",       # dove inviare gli item
    "config_path": "/api/agent/config",       # da dove scaricare gli Scout (Fase 2)
    "status_path": "/api/agent/status",       # dove inviare il progress (Fase 2)
    "interval_minutes": 60,                   # ogni quanto girare
    "days_limit": 7,                          # ignora contenuti più vecchi (0 = nessun limite)
    # NOTA: con un SaaS accoppiato gli Scout arrivano dal server (server-authoritative);
    # questi "agents" locali sono solo un fallback se il server è irraggiungibile.
    "agents": [
        {
            "name": "Scout AI",
            "keywords": ["intelligenza artificiale", "AI", "LLM"],
            "rss_feeds": [],
            "reddit_subreddits": [],
            "youtube_channels": []
        }
    ],
    "llm_bridge": {
        "enabled": False,
        "provider": "ollama",                 # ollama | lmstudio | deepseek | custom (OpenAI-compatibili)
        "endpoint": "http://localhost:11434", # locale, tunnel o URL provider remoto
        "model": "",                          # nome modello (vuoto = default per il provider)
        "api_key": "",                        # per provider remoti (es. DeepSeek) o tunnel protetti
        "poll_path": "/api/agent/jobs",       # da dove prelevare i job del SaaS
        "result_path": "/api/agent/jobs/result"
    }
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_CONFIG)
    # merge con i default (per chiavi nuove aggiunte in futuro)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    for k, v in DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            merged[k] = {**v, **(data.get(k) or {})}
    return merged


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen_urls": []}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # tiene al massimo le ultime 5000 URL viste (evita crescita infinita)
    seen = state.get("seen_urls", [])
    if len(seen) > 5000:
        state["seen_urls"] = seen[-5000:]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
