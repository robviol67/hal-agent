"""Orchestrazione: un singolo giro di raccolta+invio, e il loop periodico."""
import logging
import time
import threading

from . import config as cfg
from . import fetcher
from . import sender

log = logging.getLogger("hal_agent.runner")


def run_once(dry_run: bool = False, on_progress=None) -> dict:
    """Esegue tutti gli agenti configurati una volta e invia le novità."""
    conf = cfg.load_config()
    state = cfg.load_state()
    all_new, all_raw = [], 0

    agents = conf.get("agents", [])
    for ai, agent in enumerate(agents):
        def prog(idx, total, kind, target):
            if on_progress:
                on_progress(f"{agent.get('name','')}: {kind} {idx+1}/{total}")
        items = fetcher.run_agent(agent, days_limit=conf.get("days_limit", 0), progress=prog)
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
