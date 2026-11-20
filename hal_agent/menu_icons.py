"""
Icone e intestazioni di sezione nel menu della barra (v0.9.6).

pystray su macOS crea voci di solo testo. Qui si allarga la sua _create_menu_item:
  · una voce con l'attributo `sf` riceve l'icona SF Symbols di macOS (monocroma,
    «template», segue chiaro/scuro come le icone dei menu di sistema);
  · una voce con `section=True` diventa un'intestazione di sezione
    (NSMenuItem.sectionHeaderWithTitle_ da macOS 14; prima, una voce grigia).
Su Windows non fa niente. Se qualcosa non va, il menu resta com'era.

    from .menu_icons import item, header
    item("Allinea Giano ora", on_sync, sf="arrow.triangle.2.circlepath")
    header("Giano")
"""
import logging
import sys

log = logging.getLogger("hal_agent.menu_icons")
_patched = False


def item(text, action=None, sf=None, **kw):
    from pystray import MenuItem
    it = MenuItem(text, action or (lambda icon, i: None), **kw)
    it.sf = sf
    return it


def header(text):
    from pystray import MenuItem
    it = MenuItem(text, lambda icon, i: None, enabled=False)
    it.section = True
    return it


def install() -> None:
    """Da chiamare prima di creare l'Icon di pystray."""
    global _patched
    if _patched or sys.platform != "darwin":
        return
    try:
        import AppKit
        from pystray import _darwin
    except Exception as e:
        log.debug("icone del menu non disponibili: %s", e)
        return
    orig = _darwin.Icon._create_menu_item
    cache = {}

    def image(name):
        if name not in cache:
            img = None
            try:
                img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
                if img is not None:
                    img.setTemplate_(True)
            except Exception:
                img = None
            cache[name] = img
        return cache[name]

    def create(self, descriptor, callbacks):
        if getattr(descriptor, "section", False):
            try:
                if hasattr(AppKit.NSMenuItem, "sectionHeaderWithTitle_"):
                    mi = AppKit.NSMenuItem.sectionHeaderWithTitle_(descriptor.text)
                    mi.setTag_(len(callbacks))
                    return mi
            except Exception:
                pass
        mi = orig(self, descriptor, callbacks)
        name = getattr(descriptor, "sf", None)
        if name:
            img = image(name)
            if img is not None:
                try:
                    mi.setImage_(img)
                except Exception:
                    pass
        return mi

    _darwin.Icon._create_menu_item = create
    _patched = True
