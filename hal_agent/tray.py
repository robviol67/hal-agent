"""Interfaccia da barra di sistema (menu bar Mac / system tray Windows)."""
import logging
import os
import subprocess
import sys
import webbrowser

from . import config as cfg
from . import __version__
from .runner import Loop, BridgeLoop

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


def _make_icon_image(color=(24, 95, 165)):
    """Icona semplice generata al volo (cerchio con H)."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size - 4, size - 4), fill=color)
    d.text((size // 2 - 9, size // 2 - 12), "H", fill="white")
    return img


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

    def _bridge_enabled():
        return bool((cfg.load_config().get("llm_bridge") or {}).get("enabled"))

    def on_toggle_bridge(icon, item):
        c = cfg.load_config()
        br = c.setdefault("llm_bridge", {})
        br["enabled"] = not bool(br.get("enabled"))
        cfg.save_config(c)
        ep = br.get("endpoint", "http://localhost:11434")
        icon.notify(
            ("Ponte LLM ATTIVO → " + ep) if br["enabled"] else "Ponte LLM disattivato",
            "HAL Agent")

    def on_run_now(icon, item):
        loop.trigger_now()

    def on_open_config(icon, item):
        _open_config_file()

    def on_status(icon, item):
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
        icon.stop()

    freq_menu = pystray.Menu(*[
        Item(label, _make_interval_setter(mins, label),
             checked=_make_interval_checked(mins), radio=True)
        for mins, label in INTERVAL_CHOICES
    ])

    menu = pystray.Menu(
        Item(f"HAL Agent v{__version__}", lambda icon, item: None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item(lambda item: f"Stato: {status['text']}", on_status),
        Item("Esegui ora", on_run_now),
        Item("Frequenza raccolta", freq_menu),
        Item("Ponte LLM locale", on_toggle_bridge, checked=lambda item: _bridge_enabled()),
        pystray.Menu.SEPARATOR,
        Item("Verifica aggiornamenti…", on_check_updates),
        Item("Scarica release (GitHub)…", on_open_releases),
        Item("Apri configurazione", on_open_config),
        pystray.Menu.SEPARATOR,
        Item("Esci", on_quit),
    )
    icon = pystray.Icon("hal_agent", _make_icon_image(),
                        f"HAL Agent v{__version__}", menu)

    loop.start()
    bridge.start()
    icon.run()
