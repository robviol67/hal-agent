"""Entrypoint CLI dell'agente HAL.

Uso:
  python -m hal_agent run [--once] [--dry-run] [--interval N]   raccolta (loop o singolo giro)
  python -m hal_agent tray                                      interfaccia barra di sistema
  python -m hal_agent panel                                     pannello (Scout, invii, ponte, config)
  python -m hal_agent config                                    stampa il percorso del config
  python -m hal_agent bridge [--once]                           ponte LLM locale (polling job)
  python -m hal_agent library [--once] [--dry-run]              cartella osservata → Frontiera HAL
  python -m hal_agent documents [--once] [--dry-run] [--folder] cartella osservata → Archivio documenti
  python -m hal_agent video [--once] [--dry-run] [--folder]     elenchi di link YouTube → Feed (trascritti)
  python -m hal_agent transcribe [--once]                       coda trascrizioni del sito (+ fallback Gemini)
  python -m hal_agent transcript <url|id>                       prova: stampa la trascrizione di un video
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
    sub.add_parser("panel", help="Pannello: Scout collegati, invii, ponte LLM, impostazioni")
    sub.add_parser("uidiag", help="Diagnosi finestre: perché una finestra non viene davanti")
    sub.add_parser("config", help="Percorso del file di configurazione")
    sub.add_parser("configui", help="Finestra di configurazione del Ponte LLM")
    pf = sub.add_parser("pickfolder", help="Scegli la cartella osservata (finestra di sistema)")
    pf.add_argument("--documents", action="store_true",
                    help="Sceglie la cartella dei Documenti invece di quella dei Libri")
    pf.add_argument("--video", action="store_true",
                    help="Sceglie la cartella degli elenchi di video YouTube")

    pb = sub.add_parser("bridge", help="Ponte LLM locale (Ollama/LM Studio)")
    pb.add_argument("--once", action="store_true")

    pl = sub.add_parser("library", help="Cartella osservata → Frontiera HAL")
    pl.add_argument("--once", action="store_true", help="Una scansione poi esci")
    pl.add_argument("--dry-run", action="store_true", help="Elenca i nuovi senza caricarli")

    pd = sub.add_parser("documents", help="Cartella osservata → Archivio documenti HAL")
    pd.add_argument("--once", action="store_true", help="Una scansione poi esci")
    pd.add_argument("--dry-run", action="store_true",
                    help="Converte in Markdown e mostra cosa verrebbe inviato: niente rete, "
                         "niente scritture (né config né stato)")
    pd.add_argument("--folder", help="Solo per prova: cartella da usare al posto di quella "
                                     "in configurazione (non viene salvata)")

    pv = sub.add_parser("video", help="Elenchi di link YouTube (cartella osservata) → Feed, trascritti")
    pv.add_argument("--once", action="store_true", help="Una lettura poi esci")
    pv.add_argument("--dry-run", action="store_true",
                    help="Trascrive e mostra cosa verrebbe mandato: niente rete verso HAL, niente stato")
    pv.add_argument("--folder", help="Solo per prova: cartella da usare al posto di quella in configurazione")

    pt = sub.add_parser("transcribe", help="Coda trascrizioni del sito (+ fallback Gemini)")
    pt.add_argument("--once", action="store_true", help="Un giro poi esci")

    pp = sub.add_parser("transcript", help="Prova: stampa la trascrizione di un video YouTube")
    pp.add_argument("video", help="URL o ID del video")

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

    if args.cmd == "uidiag":
        # ⚠️ Tk installa una PROPRIA sottoclasse di NSApplication: chi tocca
        # `sharedApplication` prima di lui fa terminare il processo
        # («-[NSApplication _setup:]: unrecognized selector»). Quindi qui si
        # crea prima la finestra, e solo dopo si guarda com'è messa l'app.
        import ctypes.util
        from . import uikit
        print("piattaforma:", sys.platform, "| dentro il bundle:", getattr(sys, "frozen", False))
        print("libobjc:", ctypes.util.find_library("objc"))
        print("AppKit :", ctypes.util.find_library("AppKit"))
        try:
            import tkinter as tk
            root = tk.Tk()
            root.title("HAL Agent — diagnosi finestre")
            root.geometry("360x160")
            root.update_idletasks()
        except Exception as e:
            print("Tk non parte:", type(e).__name__, e)
            return 1

        objc, app = uikit._nsapp()
        def pol():
            if not app:
                return "?"
            import ctypes as C
            send = objc.objc_msgSend
            send.restype = C.c_long; send.argtypes = [C.c_void_p, C.c_void_p]
            return send(app, objc.sel_registerName(b"activationPolicy"))
        print("NSApplication raggiungibile:", bool(app))
        print("politica con la finestra creata:", pol(), "(0 = app normale, 1 = accessoria)")
        print("macos_become_app ->", uikit.macos_become_app())
        print("politica dopo:", pol())
        uikit.macos_activate()
        root.update_idletasks()
        print("finestra ancora viva:", bool(root.winfo_exists()))
        root.after(1500, root.destroy)
        root.mainloop()
        print("fine: nessun crash.")
        return 0

    if args.cmd == "panel":
        from . import panel_window
        panel_window.open_panel()
        return 0

    if args.cmd == "pickfolder":
        from . import folder_picker
        folder_picker.open_folder_picker("video" if args.video else ("documents" if args.documents else "library"))
        return 0

    if args.cmd == "transcript":
        from . import transcripts
        vid = transcripts.extract_video_id(args.video)
        if not vid:
            print("URL o ID non riconosciuto"); return 1
        t = transcripts.get_transcript(vid)
        if t["status"] != "done":
            print(f"{t['status']}: {t['detail']}"); return 1
        print(f"# {vid} · {t['lang']} · {len(t['text'])} caratteri\n")
        print(t["text"])
        return 0

    if args.cmd == "transcribe":
        from . import transcripts
        if args.once:
            res = transcripts.poll_and_run_once(cfg.load_config(),
                                                on_progress=lambda s: logging.getLogger("hal_agent").info(s))
            print(f"\n→ in coda {res.get('jobs',0)}, con sottotitoli {res.get('done',0)}, "
                  f"senza {res.get('none',0)}, errori {res.get('error',0)}, Gemini {res.get('fallback',0)}")
            return 0
        lp = runner.TranscriptLoop(on_status=lambda s: logging.getLogger("hal_agent").info(s))
        lp.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            lp.stop()
        return 0

    if args.cmd == "video":
        from . import videos
        conf = cfg.load_config()
        if args.folder:
            conf = dict(conf)
            conf["video"] = {**(conf.get("video") or {}), "folder": args.folder}
        res = videos.scan_once(conf, dry_run=args.dry_run, refresh_playlists=True,
                               on_progress=lambda s: logging.getLogger("hal_agent").info(s))
        print(f"\n→ file {res.get('files',0)}, playlist {res.get('playlists',0)}, "
              f"link {res.get('links',0)}, nuovi {res.get('new',0)} (solo testo {res.get('text',0)}), "
              f"mandati {res.get('sent',0)}, con sottotitoli {res.get('done',0)}, senza {res.get('none',0)}, "
              f"errori {res.get('error',0)}")
        if not args.once and not args.dry_run:
            lp = runner.VideoLoop(on_status=lambda s: logging.getLogger("hal_agent").info(s))
            lp.start()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                lp.stop()
        return 0 if res.get("ok") else 1

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

    if args.cmd == "library":
        from . import library
        res = library.scan_once(
            dry_run=args.dry_run,
            on_progress=lambda s: logging.getLogger("hal_agent").info(s))
        print(f"\n→ scansionati {res.get('scanned',0)}, nuovi {res.get('new',0)}, "
              f"inviati {res.get('uploaded',0)}, già presenti {res.get('duplicate',0)}, "
              f"errori {res.get('error',0)}")
        if not args.once:
            # senza --once resta in ascolto ripetendo a intervallo (loop bloccante)
            lp = runner.LibraryLoop(on_status=lambda s: logging.getLogger("hal_agent").info(s))
            lp.start()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                lp.stop()
        return 0 if res.get("ok") else 1

    if args.cmd == "documents":
        from . import documents
        conf = cfg.load_config()
        if args.folder:
            # override solo in memoria: la config sul disco non viene toccata
            conf = dict(conf)
            conf["documents"] = {**(conf.get("documents") or {}), "folder": args.folder}
        res = documents.scan_once(
            conf,
            dry_run=args.dry_run,
            on_progress=lambda s: logging.getLogger("hal_agent").info(s))
        print(f"\n→ scansionati {res.get('scanned',0)}, nuovi {res.get('new',0)}, "
              f"convertiti {res.get('converted',0)}, inviati {res.get('uploaded',0)}, "
              f"già presenti {res.get('known',0) + res.get('duplicate',0)}, "
              f"errori {res.get('error',0)}")
        if not args.once and not args.dry_run:
            # senza --once resta in ascolto ripetendo a intervallo (loop bloccante)
            lp = runner.DocumentsLoop(on_status=lambda s: logging.getLogger("hal_agent").info(s))
            lp.start()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                lp.stop()
        return 0 if res.get("ok") else 1

    if args.cmd == "tray" or args.cmd is None:
        from . import tray
        tray.run_tray()
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
