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


SYMBOL_PT = 16.0       # grandezza delle icone SF Symbols (v0.9.8: prima erano quelle di default, troppo piccole)
MARK_PT = 18           # lato del marchio di Giano nel menu


def item(text, action=None, sf=None, mark=None, **kw):
    """sf: nome SF Symbols · mark: 'giano' per il marchio ufficiale a colori."""
    from pystray import MenuItem
    it = MenuItem(text, action or (lambda icon, i: None), **kw)
    it.sf = sf
    it.mark = mark
    return it


def giano_mark_png(px: int = 72) -> bytes:
    """Il marchio di Giano (TOKENS.md): quadrato nero raggio 220/1024, due archi bianchi «( )»
    tratto 96/1024, punto rosso #ff151f al centro. Disegnato con Pillow, 4× e ridotto."""
    import io
    from PIL import Image, ImageDraw
    S = px * 4
    k = S / 1024
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(220 * k), fill=(17, 17, 17, 255))
    w = 96 * k
    import math
    # dall'SVG ufficiale: archi di raggio 260 fra y=270 e y=754, sulle corde x=430 e x=594;
    # il centro sta a 95 (= √(260²−242²)) dalla corda, dalla parte opposta alla pancia
    half = math.degrees(math.atan2(242, 95))          # ≈ 68,6°
    for cx, a0, a1 in ((430 + 95, 180 - half, 180 + half), (594 - 95, -half, half)):
        for i in range(0, 241):                         # tratto con estremi tondi: tanti dischi lungo l'arco
            ang = math.radians(a0 + (a1 - a0) * i / 240)
            x, y = (cx + 260 * math.cos(ang)) * k, (512 + 260 * math.sin(ang)) * k
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=(255, 255, 255, 255))
    r = 66 * k
    d.ellipse([512 * k - r, 512 * k - r, 512 * k + r, 512 * k + r], fill=(255, 21, 31, 255))
    img = img.resize((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


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
                    try:
                        conf = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(SYMBOL_PT, 0.0)
                        img = img.imageWithSymbolConfiguration_(conf) or img
                    except Exception:
                        pass
                    img.setTemplate_(True)
            except Exception:
                img = None
            cache[name] = img
        return cache[name]

    def mark(name):
        key = "mark:" + name
        if key not in cache:
            img = None
            try:
                import Foundation
                data = giano_mark_png(MARK_PT * 4)
                img = AppKit.NSImage.alloc().initWithData_(Foundation.NSData.dataWithBytes_length_(data, len(data)))
                img.setSize_((MARK_PT, MARK_PT))
                img.setTemplate_(False)       # il marchio resta a colori
            except Exception as e:
                log.debug("marchio di Giano non disegnato: %s", e)
            cache[key] = img
        return cache[key]

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
        mk = getattr(descriptor, "mark", None)
        if mk or name:
            img = mark(mk) if mk else image(name)
            if img is not None:
                try:
                    mi.setImage_(img)
                except Exception:
                    pass
        return mi

    _darwin.Icon._create_menu_item = create
    _patched = True
