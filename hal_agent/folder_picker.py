"""Selettore nativo della «cartella osservata» (capability Libreria).

Apre una finestra di scelta cartella del sistema (Tk askdirectory) e salva il
percorso in config.library.folder — così l'utente non deve editare il JSON a mano.
Gira in un PROCESSO SEPARATO (comando `pickfolder`): Tk vuole il suo main-loop,
come già per `panel`/`configui`.
"""
import logging
import os

from . import config as cfg

log = logging.getLogger("hal_agent.folder_picker")


def open_folder_picker() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)   # su macOS il dialog tende a finire dietro
    except Exception:
        pass

    c = cfg.load_config()
    lib = c.setdefault("library", {})
    current = str(lib.get("folder") or "")
    initial = current if (current and os.path.isdir(current)) else os.path.expanduser("~")

    folder = filedialog.askdirectory(
        title="Scegli la cartella dei libri da osservare",
        initialdir=initial,
        mustexist=True,
    )

    if folder:
        lib["folder"] = folder
        # se la cartella viene scelta, ha senso che la Libreria sia attiva
        lib["enabled"] = True
        cfg.save_config(c)
        log.info("Cartella osservata impostata: %s", folder)
        try:
            messagebox.showinfo(
                "HAL Agent",
                "Cartella osservata impostata:\n" + folder +
                "\n\nLa Libreria è attiva: userà «Scansiona ora» o il prossimo giro automatico.",
            )
        except Exception:
            pass

    try:
        root.destroy()
    except Exception:
        pass
