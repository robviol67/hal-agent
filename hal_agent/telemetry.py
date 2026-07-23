"""
Diario locale dell'agente: cosa ha fatto, quando, con quali Scout.

Serve al Pannello (che gira in un PROCESSO SEPARATO dalla menu-bar e quindi non
può leggere le variabili in memoria del loop): tutto ciò che il runner combina
viene scritto qui su disco, e il pannello lo rilegge ogni paio di secondi.

File: ~/.hal-agent/runtime.json
  status      riga di stato corrente + timestamp
  next_run_ts quando è prevista la prossima raccolta
  runs        ultimi N giri (totali + dettaglio per Scout)
  scouts      contatori cumulativi per Scout (nome -> totali)
  bridge      stato del ponte LLM (raggiungibilità + job eseguiti)

Il trigger di raccolta manuale è un file a parte (~/.hal-agent/trigger) così il
pannello non deve toccare runtime.json mentre il runner ci scrive.
"""
import json
import logging
import os
import time

from . import config as cfg

log = logging.getLogger("hal_agent.telemetry")

RUNTIME_PATH = cfg.CONFIG_DIR / "runtime.json"
TRIGGER_PATH = cfg.CONFIG_DIR / "trigger"

MAX_RUNS = 30          # quanti giri teniamo nello storico
EMPTY = {"status": {}, "runs": [], "scouts": {}, "bridge": {}, "next_run_ts": 0}


def read() -> dict:
    """Stato corrente (dict sempre valido, anche se il file manca o è corrotto)."""
    try:
        data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out = dict(EMPTY)
            out.update(data)
            return out
    except Exception:
        pass
    return dict(EMPTY)


def _write(data: dict) -> None:
    try:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RUNTIME_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, RUNTIME_PATH)   # sostituzione atomica: mai file a metà
    except Exception as e:
        log.debug("runtime.json non salvato: %s", e)


def update(fn) -> None:
    """Legge, applica fn(data) e riscrive."""
    data = read()
    try:
        fn(data)
    except Exception as e:
        log.debug("update telemetria fallito: %s", e)
        return
    _write(data)


# ─── scritture dal runner ───────────────────────────────────────────────────
def set_status(text: str) -> None:
    update(lambda d: d.__setitem__("status", {"text": text, "ts": time.time()}))


def set_next_run(ts: float) -> None:
    update(lambda d: d.__setitem__("next_run_ts", float(ts)))


def record_run(run: dict) -> None:
    """Aggiunge un giro allo storico e aggiorna i contatori per Scout."""
    def apply(d):
        runs = d.setdefault("runs", [])
        runs.append(run)
        del runs[:-MAX_RUNS]
        scouts = d.setdefault("scouts", {})
        for s in run.get("scouts", []):
            name = s.get("name") or "(senza nome)"
            acc = scouts.setdefault(name, {"total_raw": 0, "total_new": 0, "total_sent": 0, "runs": 0})
            acc["total_raw"] += int(s.get("raw", 0))
            acc["total_new"] += int(s.get("new", 0))
            acc["total_sent"] += int(s.get("sent", 0))
            acc["runs"] += 1
            acc["last_ts"] = run.get("ts")
            acc["last_raw"] = int(s.get("raw", 0))
            acc["last_new"] = int(s.get("new", 0))
            acc["last_sent"] = int(s.get("sent", 0))
            acc["last_error"] = s.get("error", "")
            acc["last_bad_sources"] = s.get("bad_sources") or []
            if s.get("sent"):
                acc["last_sent_ts"] = run.get("ts")
        d["last_run_ts"] = run.get("ts")
    update(apply)


def record_bridge_check(enabled: bool, ok: bool, endpoint: str, model: str, detail: str) -> None:
    def apply(d):
        br = d.setdefault("bridge", {})
        br.update({"enabled": bool(enabled), "ok": bool(ok), "endpoint": endpoint or "",
                   "model": model or "", "detail": detail or "", "checked_ts": time.time()})
    update(apply)


def record_bridge_job(job_id, ok: bool, chars: int, detail: str = "") -> None:
    def apply(d):
        br = d.setdefault("bridge", {})
        br["jobs_done"] = int(br.get("jobs_done", 0)) + 1
        if not ok:
            br["jobs_failed"] = int(br.get("jobs_failed", 0)) + 1
        br["last_job"] = {"id": job_id, "ok": bool(ok), "chars": int(chars),
                          "detail": (detail or "")[:200], "ts": time.time()}
    update(apply)


# ─── raccolta manuale richiesta dal pannello ────────────────────────────────
def request_run() -> None:
    """Il pannello chiede al processo menu-bar di fare subito un giro."""
    try:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TRIGGER_PATH.write_text(str(time.time()), encoding="utf-8")
    except Exception as e:
        log.debug("trigger non scritto: %s", e)


def consume_run_request() -> bool:
    """True (una volta sola) se qualcuno ha chiesto una raccolta immediata."""
    try:
        if TRIGGER_PATH.exists():
            TRIGGER_PATH.unlink()
            return True
    except Exception:
        pass
    return False
