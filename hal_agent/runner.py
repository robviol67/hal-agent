"""Orchestrazione: un singolo giro di raccolta+invio, e il loop periodico."""
import logging
import time
import threading

from . import config as cfg
from . import fetcher
from . import sender
from . import remote

log = logging.getLogger("hal_agent.runner")


def _resolve_agents(conf: dict):
    """
    Determina gli Scout da usare (server-authoritative, Fase 2):
    server -> cache -> config.json locale. Ritorna (agents, days_limit, fonte).
    """
    remote_cfg = remote.fetch_config(conf)
    source = "server"
    if remote_cfg is None:
        remote_cfg = remote.load_cached_config()
        source = "cache"
    if remote_cfg is not None and isinstance(remote_cfg.get("agents"), list):
        return (remote_cfg["agents"],
                int(remote_cfg.get("days_limit", conf.get("days_limit", 0)) or 0),
                source)
    # nessun server né cache: fallback al config locale
    return conf.get("agents", []), int(conf.get("days_limit", 0) or 0), "local"


def run_once(dry_run: bool = False, on_progress=None) -> dict:
    """Esegue tutti gli agenti configurati una volta e invia le novità."""
    conf = cfg.load_config()
    state = cfg.load_state()
    all_new, all_raw = [], 0

    def status(msg):
        if on_progress:
            on_progress(msg)
        if not dry_run:
            remote.send_status(conf, msg)

    agents, days_limit, source = _resolve_agents(conf)
    status(f"Raccolta avviata: {len(agents)} scout (config: {source})")

    for ai, agent in enumerate(agents):
        def prog(idx, total, kind, target, _a=agent):
            status(f"{_a.get('name','')}: {kind} {idx+1}/{total}")
        dl = int(agent.get("days_limit", days_limit) or 0)
        items = fetcher.run_agent(agent, days_limit=dl, progress=prog)
        all_raw += len(items)
        new = sender.filter_new(items, state)
        all_new.extend(new)

    result = {"raw": all_raw, "new": len(all_new)}
    if all_new:
        res = sender.send(all_new, conf, dry_run=dry_run)
        result.update(res)
        if res.get("ok") and not dry_run:
            sender.mark_sent(all_new, state)
            cfg.save_state(state)
    else:
        result["ok"] = True
        result["sent"] = 0
    status("Fatto: %d raccolti, %d nuovi, %d inviati" %
           (all_raw, len(all_new), result.get("sent", 0)))
    log.info("Giro completato: %d raccolti, %d nuovi, %d inviati",
             all_raw, len(all_new), result.get("sent", 0))
    return result


class Loop:
    """Loop in background che rilancia run_once ogni N minuti."""
    def __init__(self, on_status=None):
        self._stop = threading.Event()
        self._thread = None
        self.on_status = on_status or (lambda s: None)
        self.last_result = None

    def _status(self, msg):
        try:
            self.on_status(msg)
        except Exception:
            pass

    def _run(self):
        while not self._stop.is_set():
            conf = cfg.load_config()
            interval = max(5, int(conf.get("interval_minutes", 60))) * 60
            self._status("Raccolta in corso…")
            try:
                self.last_result = run_once(on_progress=self._status)
                self._status(f"Ultimo giro: {self.last_result.get('sent',0)} inviati")
            except Exception as e:
                log.error("Errore nel giro: %s", e)
                self._status(f"Errore: {e}")
            # attesa interrompibile
            self._stop.wait(interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def trigger_now(self):
        """Forza un giro immediato in un thread separato (non blocca la UI)."""
        threading.Thread(target=lambda: self._safe_once(), daemon=True).start()

    def _safe_once(self):
        try:
            self._status("Raccolta manuale…")
            self.last_result = run_once(on_progress=self._status)
            self._status(f"Fatto: {self.last_result.get('sent',0)} inviati")
        except Exception as e:
            self._status(f"Errore: {e}")

    def stop(self):
        self._stop.set()


class BridgeLoop:
    """
    Loop del ponte LLM: se llm_bridge.enabled è true in config, interroga il
    SaaS (GET /api/agent/jobs), esegue il job sull'LLM locale (Ollama/LM Studio)
    e rimanda il risultato. Rilegge la config a ogni giro, così si attiva/spegne
    senza riavviare l'app.
    """
    def __init__(self, on_status=None):
        self._stop = threading.Event()
        self._thread = None
        self.on_status = on_status or (lambda s: None)

    def _status(self, msg):
        try:
            self.on_status(msg)
        except Exception:
            pass

    def _run(self):
        from . import llm_bridge
        while not self._stop.is_set():
            conf = cfg.load_config()
            br = conf.get("llm_bridge", {}) or {}
            if not br.get("enabled"):
                self._stop.wait(15)   # spento: ricontrolla la config ogni tanto
                continue
            worked = False
            try:
                worked = llm_bridge.poll_and_run_once(conf)
                if worked:
                    self._status("Ponte LLM: job eseguito")
            except Exception as e:
                log.debug("Ponte LLM errore: %s", e)
            # se ha lavorato, riprova subito (potrebbero esserci altri job)
            self._stop.wait(2 if worked else 5)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
