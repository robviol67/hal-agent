"""Interfaccia da barra di sistema (menu bar Mac / system tray Windows)."""
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser

from . import config as cfg
from . import __version__
from .runner import Loop, BridgeLoop, LibraryLoop, DocumentsLoop, TranscriptLoop, VideoLoop, ConvertLoop, GianoLoop

log = logging.getLogger("hal_agent.tray")

REPO = "robviol67/hal-agent"
RELEASES_URL = f"https://github.com/{REPO}/releases"

# Opzioni di frequenza raccolta (minuti, etichetta)
INTERVAL_CHOICES = [
    (15, "15 minuti"),
    (30, "30 minuti"),
    (60, "1 ora"),
    (120, "2 ore"),
    (360, "6 ore"),
    (720, "12 ore"),
]


def _make_icon_image():
    """Il marchio di HAL (quadrato scuro, A bianca, pallino rosso), disegnato al volo."""
    from .branding import hal_mark
    return hal_mark(64)


def _open_config_file():
    path = str(cfg.CONFIG_PATH)
    cfg.load_config()  # assicura che esista
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        webbrowser.open("file://" + path)


def _parse_version(v: str):
    """'v0.2.1' / '0.2.1' -> (0, 2, 1) per confronto; parti non numeriche = 0."""
    v = (v or "").lstrip("vV").strip()
    out = []
    for part in v.split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out) if out else (0,)


def _short(text: str, n: int = 52) -> str:
    """Testo per il menu (v0.9.8): percorsi leggibili e lunghezza fissa, così il menu non si allarga.
    «/Users/x/Library/Mobile Documents/com~apple~CloudDocs/HAL/libri» → «iCloud Drive/HAL/libri»."""
    s = str(text or "")
    home = os.path.expanduser("~")
    s = s.replace(home + "/Library/Mobile Documents/com~apple~CloudDocs", "iCloud Drive")
    s = s.replace(home, "~")
    if len(s) > n:
        # un percorso lungo (dal suo inizio fino in fondo) diventa «inizio/…/ultime/due»
        idx = [i for i in (s.find("iCloud Drive/"), s.find("~/"), s.find(" /") + 1 if " /" in s else -1, 0 if s.startswith("/") else -1) if i >= 0]
        if idx:
            i = min(idx)
            parts = s[i:].split("/")
            if len(parts) > 3:
                s = s[:i] + parts[0] + "/…/" + "/".join(parts[-2:])
            if len(s) > n and len(parts) > 1:
                s = s[:i] + "…/" + parts[-1]
    if len(s) > n:
        keep = n - 1
        s = s[: keep * 2 // 3] + "…" + s[-(keep - keep * 2 // 3):]
    return s


def _latest_release():
    """Tag dell'ultima release su GitHub, o None se non raggiungibile."""
    try:
        import httpx
        r = httpx.get(f"https://api.github.com/repos/{REPO}/releases/latest",
                      headers={"Accept": "application/vnd.github+json"}, timeout=15)
        r.raise_for_status()
        return (r.json() or {}).get("tag_name")
    except Exception as e:
        log.debug("check release fallito: %s", e)
        return None


def run_tray():
    import pystray
    from pystray import MenuItem as Item

    status = {"text": "In avvio…"}
    loop = Loop(on_status=lambda s: status.__setitem__("text", s))
    bridge = BridgeLoop(on_status=lambda s: status.__setitem__("text", s))
    library = LibraryLoop(on_status=lambda s: status.__setitem__("text", s))
    documents = DocumentsLoop(on_status=lambda s: status.__setitem__("text", s))
    transcripts = TranscriptLoop(on_status=lambda s: status.__setitem__("text", s))
    video = VideoLoop(on_status=lambda s: status.__setitem__("text", s))
    converter = ConvertLoop(on_status=lambda s: status.__setitem__("text", s))
    holder = {"icon": None}

    def _notify(msg, title="HAL Agent · Giano"):
        if holder["icon"] is not None:
            holder["icon"].notify(msg, title)
    giano_loop = GianoLoop(on_status=lambda s: status.__setitem__("text", s), on_notify=_notify)

    def _giano_enabled():
        return bool((cfg.load_config().get("giano") or {}).get("enabled"))

    def on_toggle_giano(icon, item):
        c = cfg.load_config()
        g = c.setdefault("giano", {})
        g["enabled"] = not bool(g.get("enabled"))
        cfg.save_config(c)
        if g["enabled"]:
            os.makedirs(os.path.expanduser(g.get("folder") or "~/Giano"), exist_ok=True)
            icon.notify("Giano ATTIVO: i file della cartella salgono a Giano; nomi e posizioni "
                        "li decide Giano. Se modifichi un file già inviato, Giano ti chiede "
                        "«aggiorna o duplica?».", "HAL Agent")
            giano_loop.trigger_now()
        else:
            icon.notify("Giano spento: la cartella non viene più allineata.", "HAL Agent")

    def on_sync_giano(icon, item):
        if not (cfg.load_config().get("token")):
            icon.notify("Configura prima il collegamento (server + token).", "HAL Agent")
            return
        giano_loop.trigger_now()
        icon.notify("Giano: allineamento avviato.", "HAL Agent")

    def on_pick_giano_folder(icon, item):
        if not _open_ui("pickfolder", "--giano"):
            _open_config_file()

    def on_open_giano_folder(icon, item):
        folder = os.path.expanduser((cfg.load_config().get("giano") or {}).get("folder") or "~/Giano")
        os.makedirs(folder, exist_ok=True)
        try:
            import subprocess
            subprocess.Popen(["open", folder])
        except Exception:
            pass

    def _bridge_enabled():
        return bool((cfg.load_config().get("llm_bridge") or {}).get("enabled"))

    def _library_enabled():
        return bool((cfg.load_config().get("library") or {}).get("enabled"))

    def _documents_enabled():
        return bool((cfg.load_config().get("documents") or {}).get("enabled"))

    def _video_enabled():
        return bool((cfg.load_config().get("video") or {}).get("enabled"))

    def _transcripts_enabled():
        return bool((cfg.load_config().get("transcripts") or {}).get("enabled", True))

    def _folder_label(section: str):
        folder = str((cfg.load_config().get(section) or {}).get("folder") or "")
        if not folder:
            return "Cartella: (non impostata)"
        return "Cartella: " + _short(folder, 40)

    def _library_folder_label():
        return _folder_label("library")

    def _documents_folder_label():
        return _folder_label("documents")

    def _video_folder_label():
        return _folder_label("video")

    def on_pick_folder(icon, item):
        if not _open_ui("pickfolder"):
            _open_config_file()

    def on_pick_doc_folder(icon, item):
        if not _open_ui("pickfolder", "--documents"):
            _open_config_file()

    def on_pick_video_folder(icon, item):
        if not _open_ui("pickfolder", "--video"):
            _open_config_file()

    def on_toggle_video(icon, item):
        c = cfg.load_config()
        vc = c.setdefault("video", {})
        vc["enabled"] = not bool(vc.get("enabled"))
        cfg.save_config(c)
        folder = vc.get("folder", "")
        if vc["enabled"]:
            icon.notify(f"Video ATTIVI: i file di testo in «{folder}» vengono letti come elenchi di "
                        "link YouTube (uno per riga; «[testo]» in fondo alla riga = manda solo la "
                        "trascrizione in Leggi). Ogni video nuovo viene trascritto e mandato al "
                        "Feed di HAL con il testo allegato. I file non vengono toccati.",
                        "HAL Agent")
            video.trigger_now()   # prima lettura subito
        else:
            icon.notify("Video spenti: la cartella degli elenchi non viene più letta.", "HAL Agent")

    def on_scan_video(icon, item):
        if not (cfg.load_config().get("token")):
            icon.notify("Configura prima il collegamento (server + token).", "HAL Agent")
            return
        video.trigger_now()
        icon.notify("Video: lettura degli elenchi avviata.", "HAL Agent")

    def on_toggle_transcripts(icon, item):
        c = cfg.load_config()
        tr = c.setdefault("transcripts", {})
        tr["enabled"] = not bool(tr.get("enabled", True))
        cfg.save_config(c)
        if tr["enabled"]:
            icon.notify("Trascrizioni ATTIVE: l'agente scarica i sottotitoli dei video chiesti dal sito "
                        "e sveglia Gemini per quelli senza.", "HAL Agent")
            transcripts.trigger_now()
        else:
            icon.notify("Trascrizioni spente: la coda del sito non viene più letta "
                        "(gli Scout continuano a trascrivere i loro canali).", "HAL Agent")

    def _brake_label():
        """Voce del menu che compare solo quando YouTube ci ha messo in pausa."""
        from . import transcripts as tmod
        b = tmod.brake_status()
        if not b["active"]:
            return "YouTube: nessuna pausa"
        mins = int(b["left"] // 60) + 1
        return f"⏸ YouTube in pausa ancora {mins} min — riprova ora"

    def _brake_on():
        from . import transcripts as tmod
        return tmod.brake_status()["active"]

    def on_brake_release(icon, item):
        from . import transcripts as tmod
        if not tmod.brake_status()["active"]:
            icon.notify("Nessuna pausa in corso: i sottotitoli si scaricano normalmente.", "HAL Agent")
            return
        tmod.brake_release("tolto a mano dal menu")
        transcripts.trigger_now()
        icon.notify("Freno tolto: riprovo subito a scaricare i sottotitoli. "
                    "Se YouTube ci blocca di nuovo, la pausa riparte più lunga.", "HAL Agent")

    def on_transcribe_now(icon, item):
        if not (cfg.load_config().get("token")):
            icon.notify("Configura prima il collegamento (server + token).", "HAL Agent")
            return
        transcripts.trigger_now()
        icon.notify("Trascrizioni: controllo la coda del sito.", "HAL Agent")

    def on_toggle_library(icon, item):
        c = cfg.load_config()
        lib = c.setdefault("library", {})
        lib["enabled"] = not bool(lib.get("enabled"))
        cfg.save_config(c)
        folder = lib.get("folder", "")
        if lib["enabled"]:
            icon.notify(f"Libreria ATTIVA: i libri nuovi in «{folder}» vengono rinominati "
                        "(Titolo-autore-genere-anno), inviati alla Frontiera di HAL e spostati in inviati/.",
                        "HAL Agent")
            library.trigger_now()   # prima scansione subito
        else:
            icon.notify("Libreria spenta: la cartella osservata non viene più letta.", "HAL Agent")

    def on_scan_library(icon, item):
        if not (cfg.load_config().get("token")):
            icon.notify("Configura prima il collegamento (server + token).", "HAL Agent")
            return
        library.trigger_now()
        icon.notify("Libreria: scansione della cartella avviata.", "HAL Agent")

    def on_toggle_documents(icon, item):
        c = cfg.load_config()
        doc = c.setdefault("documents", {})
        doc["enabled"] = not bool(doc.get("enabled"))
        cfg.save_config(c)
        folder = doc.get("folder", "")
        if doc["enabled"]:
            icon.notify(f"Documenti ATTIVI: i file nuovi in «{folder}» vengono convertiti in "
                        "Markdown (in memoria, nessun file aggiunto) e inviati all'Archivio "
                        "documenti di HAL, nella coda «Da classificare». Gli originali restano "
                        "dove sono, non vengono rinominati né spostati.",
                        "HAL Agent")
            documents.trigger_now()   # prima scansione subito
        else:
            icon.notify("Documenti spenti: la cartella osservata non viene più letta.", "HAL Agent")

    def on_scan_documents(icon, item):
        if not (cfg.load_config().get("token")):
            icon.notify("Configura prima il collegamento (server + token).", "HAL Agent")
            return
        documents.trigger_now()
        icon.notify("Documenti: scansione della cartella avviata.", "HAL Agent")

    def on_toggle_bridge(icon, item):
        c = cfg.load_config()
        br = c.setdefault("llm_bridge", {})
        br["enabled"] = not bool(br.get("enabled"))
        cfg.save_config(c)
        ep = br.get("endpoint", "http://localhost:11434")
        icon.notify(
            ("Ponte LLM ATTIVO: il sito HAL può far scrivere il modello su "
             "questo computer (" + ep + ")") if br["enabled"]
            else "Ponte LLM spento: il sito HAL userà solo i provider cloud.",
            "HAL Agent")

    def _open_ui(subcmd: str, *extra, icon=None):
        """Apre una finestra Tk in un processo separato (Tk vuole il suo main-loop).

        Se la finestra muore appena nata (finora succedeva in silenzio: sembrava
        che il comando non facesse nulla) l'errore finisce in
        ~/.hal-agent/finestre.log e arriva una notifica con l'ultima riga.
        """
        try:
            errlog = cfg.CONFIG_DIR / "finestre.log"
            try:
                cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                err = open(errlog, "ab")
                err.write(("\n===== %s · %s =====\n" % (subcmd, time.strftime("%Y-%m-%d %H:%M:%S"))).encode())
                err.flush()
            except Exception:
                err = subprocess.DEVNULL
            if getattr(sys, "frozen", False):
                proc = subprocess.Popen([sys.executable, subcmd, *extra], stderr=err)
            else:
                proc = subprocess.Popen([sys.executable, "-m", "hal_agent", subcmd, *extra], stderr=err)
        except Exception as e:
            log.error("apertura finestra '%s' fallita: %s", subcmd, e)
            return False

        def _watch():
            """Se il processo esce subito con errore, dillo invece di tacere."""
            for _ in range(30):                     # ~6 secondi
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            code = proc.poll()
            if code in (None, 0):
                return
            motivo = ""
            try:
                righe = [r.strip() for r in errlog.read_text(errors="replace").splitlines() if r.strip()]
                if righe:
                    motivo = righe[-1][:180]
            except Exception:
                pass
            log.error("la finestra '%s' si è chiusa subito (codice %s): %s", subcmd, code, motivo)
            if icon is not None:
                try:
                    icon.notify("La finestra non si è aperta. Dettagli in "
                                "~/.hal-agent/finestre.log\n" + motivo, "HAL Agent")
                except Exception:
                    pass
        threading.Thread(target=_watch, daemon=True).start()
        return True

    def on_open_panel(icon, item):
        if not _open_ui("panel", icon=icon):
            _open_config_file()

    def on_paste_video(icon, item):
        if not (cfg.load_config().get("token")):
            icon.notify("Configura prima il collegamento (server + token).", "HAL Agent")
            return
        _open_ui("paste", icon=icon)

    def on_configure_bridge(icon, item):
        if not _open_ui("configui", icon=icon):
            _open_config_file()

    def on_run_now(icon, item):
        loop.trigger_now()

    def on_open_config(icon, item):
        _open_config_file()

    def on_status(icon, item):
        # Il click sullo stato apre il pannello: lì si vedono Scout, invii e ponte.
        if not _open_ui("panel", icon=icon):
            icon.notify(status["text"], "HAL Agent")

    def on_open_releases(icon, item):
        webbrowser.open(RELEASES_URL)

    def on_check_updates(icon, item):
        latest = _latest_release()
        if not latest:
            icon.notify("Impossibile verificare (nessuna connessione?)", "HAL Agent")
            return
        if _parse_version(latest) > _parse_version(__version__):
            icon.notify(f"Aggiornamento disponibile: {latest} (hai v{__version__}). "
                        f"Apro la pagina Release…", "HAL Agent")
            webbrowser.open(RELEASES_URL)
        else:
            icon.notify(f"Sei aggiornato (v{__version__}).", "HAL Agent")

    def _make_interval_setter(mins, label):
        def handler(icon, item):
            c = cfg.load_config()
            c["interval_minutes"] = mins
            cfg.save_config(c)
            icon.notify(f"Frequenza raccolta: ogni {label}", "HAL Agent")
        return handler

    def _make_interval_checked(mins):
        return lambda item: int(cfg.load_config().get("interval_minutes", 60)) == mins

    def on_quit(icon, item):
        loop.stop()
        bridge.stop()
        library.stop()
        documents.stop()
        transcripts.stop()
        video.stop()
        converter.stop()
        giano_loop.stop()
        icon.stop()

    freq_menu = pystray.Menu(*[
        Item(label, _make_interval_setter(mins, label),
             checked=_make_interval_checked(mins), radio=True)
        for mins, label in INTERVAL_CHOICES
    ])

    # v0.9.6: icone SF Symbols e intestazioni di sezione (menu_icons.py)
    from . import menu_icons
    from .menu_icons import item as I, header as H
    menu_icons.install()

    avanzate = pystray.Menu(
        I("Configura solo il ponte LLM…", on_configure_bridge, sf="slider.horizontal.3"),
        I("Apri il file di configurazione (JSON)", on_open_config, sf="curlybraces"),
        I("Scarica release (GitHub)…", on_open_releases, sf="shippingbox"),
    )

    menu = pystray.Menu(
        I(f"HAL Agent v{__version__}", None, sf="sparkles", enabled=False),
        I(lambda item: "Stato: " + _short(status['text'], 52), on_status, sf="waveform.path.ecg"),
        I("Apri pannello…", on_open_panel, sf="macwindow", default=True),
        H("Giano"),
        I("Giano (cartella del Mac)", on_toggle_giano, mark="giano",
          checked=lambda item: _giano_enabled()),
        I(lambda item: _folder_label("giano"), on_open_giano_folder, sf="folder"),
        I("Scegli la cartella di Giano…", on_pick_giano_folder, sf="folder.badge.gearshape"),
        I("Allinea Giano ora", on_sync_giano, sf="arrow.triangle.2.circlepath"),
        I("Pannello di Giano…", on_open_panel, sf="macwindow.on.rectangle"),
        H("Raccolta per HAL"),
        I("Raccogli ora", on_run_now, sf="arrow.down.circle"),
        I("Frequenza raccolta", freq_menu, sf="clock"),
        I("Incolla un video YouTube…", on_paste_video, sf="play.rectangle.on.rectangle"),
        I("Ponte LLM (modello locale)", on_toggle_bridge, sf="cpu",
          checked=lambda item: _bridge_enabled()),
        H("Libreria"),
        I("Libreria (cartella osservata)", on_toggle_library, sf="books.vertical",
          checked=lambda item: _library_enabled()),
        I(lambda item: _library_folder_label(), on_pick_folder, sf="folder"),
        I("Scegli la cartella osservata…", on_pick_folder, sf="folder.badge.gearshape"),
        I("Scansiona la cartella ora", on_scan_library, sf="magnifyingglass"),
        H("Documenti"),
        I("Documenti (cartella osservata)", on_toggle_documents, sf="doc.text",
          checked=lambda item: _documents_enabled()),
        I(lambda item: _documents_folder_label(), on_pick_doc_folder, sf="folder"),
        I("Scegli la cartella dei documenti…", on_pick_doc_folder, sf="folder.badge.gearshape"),
        I("Scansiona documenti ora", on_scan_documents, sf="magnifyingglass"),
        H("Video e trascrizioni"),
        I("Video (cartella di elenchi YouTube)", on_toggle_video, sf="play.rectangle",
          checked=lambda item: _video_enabled()),
        I(lambda item: _video_folder_label(), on_pick_video_folder, sf="folder"),
        I("Scegli la cartella dei video…", on_pick_video_folder, sf="folder.badge.gearshape"),
        I("Leggi gli elenchi ora", on_scan_video, sf="list.bullet"),
        I("Trascrizioni per il sito (coda + Gemini)", on_toggle_transcripts, sf="captions.bubble",
          checked=lambda item: _transcripts_enabled()),
        I("Controlla la coda ora", on_transcribe_now, sf="tray.and.arrow.down"),
        I(lambda item: _brake_label(), on_brake_release, sf="pause.circle", visible=lambda item: _brake_on()),
        pystray.Menu.SEPARATOR,
        I("Verifica aggiornamenti…", on_check_updates, sf="arrow.down.app"),
        I("Avanzate", avanzate, sf="gearshape"),
        pystray.Menu.SEPARATOR,
        I("Esci", on_quit, sf="power"),
    )
    icon = pystray.Icon("hal_agent", _make_icon_image(),
                        f"HAL Agent v{__version__}", menu)
    holder["icon"] = icon

    def _update_watch():
        # v0.9.5: ogni 6 ore guarda le Release; una notifica per ogni versione nuova
        import threading as _th
        from . import updates
        stop = _th.Event()
        stop.wait(60)
        while True:
            try:
                tag = updates.check(_latest_release)
                if tag:
                    _notify(f"È uscita la versione {tag} di HAL Agent (hai la v{__version__}). "
                            "La trovi in «Verifica aggiornamenti…» o nel pannello.", "HAL Agent")
            except Exception as e:
                log.debug("controllo aggiornamenti: %s", e)
            stop.wait(1800)
    threading.Thread(target=_update_watch, daemon=True).start()

    loop.start()
    bridge.start()
    library.start()
    documents.start()
    transcripts.start()
    video.start()
    converter.start()
    giano_loop.start()
    icon.run()
