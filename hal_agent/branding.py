"""Il marchio di HAL, disegnato invece che caricato.

È lo stesso segno del sito (`hal_logo_mark()` in lib/layout.php): quadrato
scuro con gli angoli tondi, la «A» senza traversa in bianco e il pallino rosso
al centro. Ridisegnarlo con PIL, invece di portarsi dietro dei PNG, vuol dire
averlo nitido a qualunque misura — dalla menu-bar (22 px) all'icona dell'app
(1024 px) — e un marchio solo da cambiare se un giorno cambia.

Coordinate originali (tela 1024×1024):
    riquadro   rect 0,0 1024×1024  raggio 220  #111
    lettera    M270,740 L512,280 L754,740  tratto 92, estremi e vertice tondi
    pallino    cerchio (512, 625) raggio 70  #ff151f
"""
BG = (17, 17, 17, 255)          # #111
INK = (255, 255, 255, 255)
DOT = (255, 21, 31, 255)        # #ff151f


def hal_mark(size: int = 512, bg=BG, ink=INK, dot=DOT, radius_ratio: float = 220 / 1024):
    """Immagine RGBA del marchio, quadrata. Disegnata in grande e rimpicciolita
    (4×) perché i bordi restino lisci anche a 22 px."""
    from PIL import Image, ImageDraw

    ss = 4                                   # sovracampionamento
    S = max(16, int(size)) * ss
    k = S / 1024.0                           # dalle coordinate originali ai pixel

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(1024 * radius_ratio * k), fill=bg)

    # la «A»: due segmenti con vertice tondo, più le estremità arrotondate a mano
    w = 92 * k
    pts = [(270 * k, 740 * k), (512 * k, 280 * k), (754 * k, 740 * k)]
    d.line(pts, fill=ink, width=int(round(w)), joint="curve")
    r = w / 2.0
    for (x, y) in (pts[0], pts[2]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=ink)

    # il pallino rosso
    cx, cy, rr = 512 * k, 625 * k, 70 * k
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=dot)

    return img.resize((max(16, int(size)), max(16, int(size))), Image.LANCZOS)


def save_png(path: str, size: int) -> str:
    hal_mark(size).save(path, "PNG")
    return path
