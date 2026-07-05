"""Entrypoint CLI dell'agente HAL.

Uso:
  python -m hal_agent run [--once] [--dry-run] [--interval N]   raccolta (loop o singolo giro)
  python -m hal_agent tray                                      interfaccia barra di sistema
  python -m hal_agent config                                    stampa il percorso del config
  python -m hal_agent bridge [--once]                           ponte LLM locale (polling job)
"""
import argparse
import logging
import sys
import time

from . import __version__
from . import config as cfg
from . import runner


def _setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv=None):
    p = argparse.ArgumentParser(prog="hal_agent", description="HAL Agent desktop")
    p.add_argument("--version", action="version", version=f"HAL Agent {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="Raccolta e invio")
    pr.add_argument("--once", action="store_true", help="Un solo giro poi esci")
    pr.add_argument("--dry-run", action="store_true", help="Stampa il payload invece di inviarlo")
    pr.add_argument("--interval", type=int, help="Override intervallo minuti (loop)")

    sub.add_parser("tray", help="Interfaccia barra di sistema")
    sub.add_parser("config", help="Percorso del file di configurazione")
    sub.add_parser("configui", help="Finestra di configurazione del Ponte LLM")

    pb = sub.add_parser("bridge", help="Ponte LLM locale (Ollama/LM Studio)")
    pb.add_argument("--once", action="store_true")

    args = p.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "config":
        cfg.load_config()
        print(cfg.CONFIG_PATH)
        return 0

    if args.cmd == "configui":
        from . import config_window
        config_window.open_config_window()
        return 0

    if args.cmd == "run":
        if args.interval:
            c = cfg.load_config(); c["interval_minutes"] = args.interval; cfg.save_config(c)
        if args.once:
            res = runner.run_once(dry_run=args.dry_run,
                                  on_progress=lambda s: logging.getLogger("hal_agent").info(s))
            print(f"\n→ raccolti {res.get('raw',0)}, nuovi {res.get('new',0)}, inviati {res.get('sent',0)}")
            return 0 if res.get("ok") else 1
        # loop bloccante
        loop = runner.Loop(on_status=lambda s: logging.getLogger("hal_agent").info(s))
        loop.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            loop.stop()
        return 0

    if args.cmd == "bridge":
        from . import llm_bridge
        conf = cfg.load_config()
        if args.once:
            worked = llm_bridge.poll_and_run_once(conf)
            print("job eseguito" if worked else "nessun job")
            return 0
        while True:
            try:
                if not llm_bridge.poll_and_run_once(cfg.load_config()):
                    time.sleep(5)
            except KeyboardInterrupt:
                return 0

    if args.cmd == "tray" or args.cmd is None:
        from . import tray
        tray.run_tray()
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
