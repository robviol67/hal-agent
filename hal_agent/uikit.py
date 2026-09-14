"""Far comparire davvero una finestra su macOS.

L'agente è un'app «accessory» (`LSUIElement`): vive nella menu-bar, non ha
icona nel Dock. Ottimo per lui, pessimo per le finestre che apre: un processo
accessory non viene mai attivato, così la finestra nasce DIETRO a quelle degli
altri programmi e — su macOS recenti — Tk la disegna pure vuota, un rettangolo
grigio, perché non riceve mai il primo evento di disegno.

Qui si rimedia in tre mosse, tutte facoltative e protette: se una fallisce si
prosegue con le altre.
  1. **a finestra già creata** si promuove il processo ad app normale
     (NSApplicationActivationPolicyRegular) parlando col runtime Objective-C via
     ctypes: niente dipendenze in più. Farlo PRIMA di Tk fa crashare AppKit;
  2. si porta la finestra davanti e le si dà il fuoco;
  3. si dà una «spintarella» alla geometria, che costringe Tk a ridisegnare.
"""
import ctypes
import ctypes.util
import logging
import os
import re
import subprocess
import sys

log = logging.getLogger("hal_agent.uikit")

_NS_ACTIVATION_POLICY_REGULAR = 0


def _nsapp():
    """(objc, NSApplication condivisa) oppure (None, None) se qualcosa non va."""
    if sys.platform != "darwin":
        return None, None
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("AppKit"))   # carica il framework
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        cls = objc.objc_getClass(b"NSApplication")
        if not cls:
            return None, None
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        app = objc.objc_msgSend(cls, objc.sel_registerName(b"sharedApplication"))
        return (objc, app) if app else (None, None)
    except Exception as e:
        log.debug("NSApplication non raggiungibile: %s", e)
        return None, None


def macos_become_app() -> bool:
    """
    Da app di menu-bar a app normale, per questo solo processo: è ciò che
    permette alla finestra di venire davanti e di essere disegnata.
    True se alla fine il processo è «regular» — anche se lo era già.
    ⚠️ Va chiamata DOPO aver creato la finestra Tk: prima, AppKit termina il
    processo con un'eccezione.
    """
    objc, app = _nsapp()
    if not app:
        return False
    try:
        send = objc.objc_msgSend
        send.restype = ctypes.c_bool
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(app, objc.sel_registerName(b"setActivationPolicy:"), _NS_ACTIVATION_POLICY_REGULAR)
        # il valore di ritorno è NO anche quando la politica era GIÀ quella giusta:
        # a dire la verità è solo lo stato finale
        send.restype = ctypes.c_long
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return int(send(app, objc.sel_registerName(b"activationPolicy"))) == _NS_ACTIVATION_POLICY_REGULAR
    except Exception as e:
        log.debug("cambio di politica non riuscito: %s", e)
        return False


def macos_activate() -> None:
    """Porta l'app davanti a tutte. Ha effetto quando la finestra esiste già."""
    objc, app = _nsapp()
    if not app:
        return
    try:
        send = objc.objc_msgSend
        send.restype = None
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        send(app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    except Exception as e:
        log.debug("attivazione non riuscita: %s", e)


def _activate_with_osascript() -> None:
    """Ripiego: chiede a System Events di portare davanti questo processo."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to set frontmost of '
             '(first process whose unix id is %d) to true' % os.getpid()],
            capture_output=True, timeout=5)
    except Exception as e:
        log.debug("attivazione via osascript non riuscita: %s", e)


def bring_to_front(root) -> None:
    """Porta davanti la finestra Tk `root` e la costringe a disegnarsi."""
    try:
        root.update_idletasks()
    except Exception:
        pass
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(500, lambda: _safe(root.attributes, "-topmost", False))
        root.focus_force()
    except Exception as e:
        log.debug("lift/focus: %s", e)

    root.after(40, _promote_and_activate)   # ora la finestra c'è: si può promuovere e attivare
    root.after(120, _activate_with_osascript)   # ripiego, se il modo diretto non bastasse
    root.after(150, lambda: _nudge(root))
    root.after(700, lambda: _nudge(root))      # una seconda, dopo l'attivazione


def _promote_and_activate() -> None:
    """
    Prima si promuove il processo ad app normale, poi lo si porta davanti.
    L'ordine conta e non è quello che verrebbe in mente: promuovere PRIMA di
    creare la finestra fa terminare il processo con un'eccezione di AppKit
    (Tk vuole trovarsi l'NSApplication com'era all'avvio). Dopo, invece, va.
    """
    ok = macos_become_app()
    macos_activate()
    log.info("finestra: app normale=%s, attivazione richiesta", ok)


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def _nudge(root) -> None:
    """Un pixel avanti e indietro: sblocca il disegno di una finestra grigia."""
    try:
        m = re.match(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", root.geometry())
        if not m:
            return
        w, h, x, y = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        root.geometry(f"{w + 1}x{h}{x}{y}")
        root.update_idletasks()
        root.geometry(f"{w}x{h}{x}{y}")
        root.update_idletasks()
    except Exception as e:
        log.debug("spintarella alla geometria: %s", e)
