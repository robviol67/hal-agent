"""Interfaccia da barra di sistema (menu bar Mac / system tray Windows)."""
import logging
import os
import subprocess
import sys
import webbrowser

from . import config as cfg
from .runner import Loop

log = logging.getLogger("hal_agent.tray")


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


def run_tray():
    import pystray
    from pystray import MenuItem as Item

    status = {"text": "In avvio…"}
    loop = Loop(on_status=lambda s: status.__setitem__("text", s))

    def on_run_now(icon, item):
        loop.trigger_now()

    def on_open_config(icon, item):
        _open_config_file()

    def on_status(icon, item):
        icon.notify(status["text"], "HAL Agent")

    def on_quit(icon, item):
        loop.stop()
        icon.stop()

    menu = pystray.Menu(
        Item(lambda item: f"Stato: {status['text']}", on_status),
        Item("Esegui ora", on_run_now),
        Item("Apri configurazione", on_open_config),
        pystray.Menu.SEPARATOR,
        Item("Esci", on_quit),
    )
    icon = pystray.Icon("hal_agent", _make_icon_image(), "HAL Agent", menu)

    loop.start()
    icon.run()
