"""Riconoscimento del testo per Giano (v0.9.0), con Vision di macOS.

ocr_pdf(entrata, uscita) legge ogni pagina di un PDF fatto di immagini (una
scansione), ne riconosce il testo e scrive un PDF nuovo IDENTICO a vista, con
sopra uno strato di testo invisibile nella posizione giusta: si può cercare,
selezionare e copiare in qualsiasi lettore. Ritorna il testo pagina per pagina.

Tutto succede sul Mac: niente servizi esterni.
"""
import logging
import sys

log = logging.getLogger("hal_agent.giano_ocr")

_LANGS = ["it-IT", "en-US", "fr-FR", "de-DE", "es-ES"]
_RENDER_LONG_SIDE = 2600     # pixel del lato lungo per la lettura (≈ 300 dpi su un A4)


class OcrError(RuntimeError):
    pass


def _render_page(Quartz, page, box):
    """La pagina come immagine (CGImage), abbastanza grande per Vision."""
    w, h = box.size.width, box.size.height
    scale = _RENDER_LONG_SIDE / max(w, h)
    pw, ph = int(w * scale), int(h * scale)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, pw, ph, 8, pw * 4, cs, Quartz.kCGImageAlphaPremultipliedLast)
    if ctx is None:
        raise OcrError("memoria insufficiente per leggere la pagina")
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, pw, ph))
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextTranslateCTM(ctx, -box.origin.x, -box.origin.y)
    Quartz.CGContextDrawPDFPage(ctx, page)
    return Quartz.CGBitmapContextCreateImage(ctx)


def _recognize(Vision, cgimage):
    """[(testo, (x, y, w, h) normalizzati con origine in basso a sinistra)]"""
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    try:
        req.setRecognitionLanguages_(_LANGS)
    except Exception:
        pass
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, {})
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise OcrError(f"Vision non ha letto la pagina: {err}")
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        text = str(cands[0].string())
        bb = obs.boundingBox()
        out.append((text, (bb.origin.x, bb.origin.y, bb.size.width, bb.size.height)))
    # in ordine di lettura: dall'alto in basso, poi da sinistra a destra
    out.sort(key=lambda t: (-round(t[1][1] + t[1][3], 2), t[1][0]))
    return out


def ocr_pdf(src: str, dst: str) -> list:
    """Riconosce il testo di src e scrive in dst il PDF ricercabile. Ritorna [testo pagina 1, …]."""
    if sys.platform != "darwin":
        raise OcrError("il riconoscimento del testo c'è solo su macOS")
    try:
        import Quartz
        import Vision
        import CoreText
        from Foundation import NSURL, NSAttributedString
    except Exception as e:
        raise OcrError(f"componenti di macOS mancanti: {e}")

    doc = Quartz.CGPDFDocumentCreateWithURL(NSURL.fileURLWithPath_(src))
    if doc is None:
        raise OcrError("PDF illeggibile")
    n = Quartz.CGPDFDocumentGetNumberOfPages(doc)
    if n == 0:
        raise OcrError("PDF senza pagine")

    first_box = Quartz.CGPDFPageGetBoxRect(Quartz.CGPDFDocumentGetPage(doc, 1), Quartz.kCGPDFMediaBox)
    out = Quartz.CGPDFContextCreateWithURL(NSURL.fileURLWithPath_(dst), first_box, None)
    if out is None:
        raise OcrError("non riesco a scrivere il PDF")
    pages_text = []
    try:
        for i in range(1, n + 1):
            page = Quartz.CGPDFDocumentGetPage(doc, i)
            box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
            lines = _recognize(Vision, _render_page(Quartz, page, box))
            pages_text.append("\n".join(t for t, _ in lines))

            Quartz.CGContextBeginPage(out, box)
            Quartz.CGContextDrawPDFPage(out, page)
            # lo strato di testo, invisibile, nella posizione di ogni riga
            Quartz.CGContextSetTextDrawingMode(out, Quartz.kCGTextInvisible)
            W, H, X0, Y0 = box.size.width, box.size.height, box.origin.x, box.origin.y
            for text, (x, y, w, h) in lines:
                if not text.strip() or w <= 0 or h <= 0:
                    continue
                size = max(2.0, h * H * 0.82)
                font = CoreText.CTFontCreateWithName("Helvetica", size, None)
                attr = NSAttributedString.alloc().initWithString_attributes_(text, {CoreText.kCTFontAttributeName: font})
                line = CoreText.CTLineCreateWithAttributedString(attr)
                natural = CoreText.CTLineGetTypographicBounds(line, None, None, None)[0] or 1.0
                sx = (w * W) / natural
                Quartz.CGContextSaveGState(out)
                Quartz.CGContextSetTextMatrix(out, Quartz.CGAffineTransformMakeScale(sx, 1.0))
                Quartz.CGContextSetTextPosition(out, X0 + x * W, Y0 + y * H + h * H * 0.18)
                CoreText.CTLineDraw(line, out)
                Quartz.CGContextRestoreGState(out)
            Quartz.CGContextEndPage(out)
    finally:
        Quartz.CGPDFContextClose(out)
    return pages_text
