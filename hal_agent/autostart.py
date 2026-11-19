"""
Avvio all'accensione (v0.9.5): un LaunchAgent dell'utente che apre «HAL Agent.app» al login.

    ~/Library/LaunchAgents/it.hal-ai.agent.plist  →  /usr/bin/open -a "/Applications/HAL Agent.app"

Si usa `open -a` (e non il binario) così l'app parte come se la si aprisse dal Finder,
una sola copia. Il file lo scrive/toglie il pannello, su richiesta dell'utente.
macOS al primo salvataggio mostra l'avviso «Elemento aggiunto agli elementi di login».
"""
import os
import plistlib
import sys
from pathlib import Path

LABEL = "it.hal-ai.agent"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def app_path() -> str:
    """Il percorso di «HAL Agent.app» se giriamo dal bundle, altrimenti ''."""
    if not getattr(sys, "frozen", False):
        return ""
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return str(parent)
    return ""


def is_enabled() -> bool:
    return PLIST.exists()


def enable() -> str:
    """Scrive il LaunchAgent. Ritorna '' se tutto bene, altrimenti il motivo."""
    app = app_path()
    if not app:
        return "funziona solo con l'app installata (HAL Agent.app), non dai sorgenti"
    if app.startswith("/Volumes/") or "/AppTranslocation/" in app:
        return "sposta prima HAL Agent.app in Applicazioni"
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": LABEL, "ProgramArguments": ["/usr/bin/open", "-a", app], "RunAtLoad": True,
            "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua"}
    with open(PLIST, "wb") as fh:
        plistlib.dump(data, fh)
    return ""


def disable() -> None:
    try:
        os.remove(PLIST)
    except FileNotFoundError:
        pass
