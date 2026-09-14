"""Selettore nativo della «cartella osservata» (capability Libreria, Documenti e Video).

Apre una finestra di scelta cartella del sistema (Tk askdirectory) e salva il
percorso in config.library.folder, config.documents.folder o config.video.folder —
così l'utente non deve editare il JSON a mano. Gira in un PROCESSO SEPARATO (comando
`pickfolder [--documents|--video]`): Tk vuole il suo main-loop, come già per
`panel`/`configui`.
"""
import logging
import os

from . import config as cfg
from . import uikit

log = logging.getLogger("hal_agent.folder_picker")

# testi per sezione: («chiave config», titolo del dialog, spiegazione finale)
_SECTIONS = {
    "library": (
        "Scegli la cartella dei libri da osservare",
        "La Libreria è attiva: userà «Scansiona ora» o il prossimo giro automatico.",
    ),
    "documents": (
        "Scegli la cartella dei documenti da osservare",
        "I Documenti sono attivi: i file nuovi verranno convertiti in Markdown e "
        "inviati all'Archivio. Gli originali restano dove sono.",
    ),
    "video": (
        "Scegli la cartella degli elenchi di video YouTube",
        "I Video sono attivi: metti qui file di testo con un link YouTube per riga. "
        "Ogni video nuovo viene trascritto e mandato al Feed. I file non vengono toccati.",
    ),
}


def open_folder_picker(section: str = "library") -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    title, note = _SECTIONS.get(section, _SECTIONS["library"])

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)   # su macOS il dialog tende a finire dietro
    except Exception:
        pass

    c = cfg.load_config()
    sec = c.setdefault(section, {})
    current = str(sec.get("folder") or "")
    initial = current if (current and os.path.isdir(current)) else os.path.expanduser("~")

    folder = filedialog.askdirectory(
        title=title,
        initialdir=initial,
        mustexist=True,
    )

    if folder:
        sec["folder"] = folder
        # se la cartella viene scelta, ha senso che la capability sia attiva
        sec["enabled"] = True
        cfg.save_config(c)
        log.info("Cartella osservata (%s) impostata: %s", section, folder)
        try:
            messagebox.showinfo(
                "HAL Agent",
                "Cartella osservata impostata:\n" + folder + "\n\n" + note,
            )
        except Exception:
            pass

    try:
        root.destroy()
    except Exception:
        pass
