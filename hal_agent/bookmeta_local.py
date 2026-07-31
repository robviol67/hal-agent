"""Estrazione LOCALE dei metadati di un libro (best-effort, senza dipendenze) e
costruzione del nome file «Titolo-autore-genere-anno.ext».

Serve alla capability Libreria per rinominare i file nella cartella osservata
prima di spedirli. La catalogazione VERA la fa comunque la Dogana lato server
(OpenLibrary/Google Books): qui puntiamo solo a un nome ordinato per la cartella
dell'utente e per la lista Frontiera.

Affidabilità per formato:
  - EPUB, DOCX: buona (metadati strutturati OPF / docProps).
  - PDF:        discreta (dizionario /Info o XMP), il genere quasi mai.
  - RTF, TXT:   scarsa → si ricade sul nome file originale + segnaposto.

Campi mancanti → segnaposto: autore «sconosciuto», genere «vario», anno «sd».
"""
import html
import os
import re
import zipfile

# segnaposto per i campi non trovati
PH_AUTHOR = "sconosciuto"
PH_GENRE = "vario"
PH_YEAR = "sd"

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')     # caratteri non ammessi nei nomi file
_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    """Ripulisce un valore di metadato: entità HTML, spazi, tag residui."""
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)          # eventuali tag annidati
    s = _WS.sub(" ", s).strip()
    return s


def _year(s: str) -> str:
    """Primo anno plausibile (1400–2099) trovato nella stringa (anche dentro date
    compatte tipo «D:20210102» o «2023-03-15»)."""
    if not s:
        return ""
    m = re.search(r"(1[4-9]\d{2}|20\d{2})", s)
    return m.group(1) if m else ""


def _xml_tag(xml: str, name: str) -> str:
    """Primo contenuto di <ns:name ...>…</ns:name> o <name ...>…</name>."""
    m = re.search(r"<(?:\w+:)?%s\b[^>]*>(.*?)</(?:\w+:)?%s>" % (name, name), xml, re.I | re.S)
    return _clean(m.group(1)) if m else ""


# ───────────────────────────── per formato ─────────────────────────────
def _from_epub(path: str) -> dict:
    with zipfile.ZipFile(path) as z:
        opf_path = "content.opf"
        try:
            cont = z.read("META-INF/container.xml").decode("utf-8", "ignore")
            m = re.search(r'full-path="([^"]+)"', cont)
            if m:
                opf_path = m.group(1)
        except KeyError:
            pass
        try:
            opf = z.read(opf_path).decode("utf-8", "ignore")
        except KeyError:
            # ripiego: primo .opf nell'archivio
            names = [n for n in z.namelist() if n.lower().endswith(".opf")]
            if not names:
                return {}
            opf = z.read(names[0]).decode("utf-8", "ignore")
    return {
        "title": _xml_tag(opf, "title"),
        "author": _xml_tag(opf, "creator"),
        "genre": _xml_tag(opf, "subject"),
        "year": _year(_xml_tag(opf, "date")),
    }


def _from_docx(path: str) -> dict:
    with zipfile.ZipFile(path) as z:
        try:
            core = z.read("docProps/core.xml").decode("utf-8", "ignore")
        except KeyError:
            return {}
    return {
        "title": _xml_tag(core, "title"),
        "author": _xml_tag(core, "creator"),
        "genre": _xml_tag(core, "subject"),
        "year": _year(_xml_tag(core, "created")),
    }


def _pdf_field(blob: bytes, key: str) -> str:
    """Valore di /Key (…) o /Key <hex> dal dizionario /Info del PDF."""
    # forma parentetica: /Title (Testo con \(escape\))
    m = re.search(rb"/%s\s*\(((?:[^()\\]|\\.)*)\)" % key.encode(), blob)
    if m:
        raw = m.group(1)
        raw = re.sub(rb"\\([()\\])", rb"\1", raw)
        # UTF-16 SOLO col BOM; altrimenti PDFDocEncoding ≈ latin-1 (ASCII incluso)
        if raw[:2] in (b"\xfe\xff", b"\xff\xfe"):
            return _clean(raw.decode("utf-16", "ignore"))
        return _clean(raw.decode("latin-1", "ignore"))
    # forma esadecimale: /Title <FEFF0054...>
    m = re.search(rb"/%s\s*<([0-9A-Fa-f]+)>" % key.encode(), blob)
    if m:
        try:
            b = bytes.fromhex(m.group(1).decode())
            enc = "utf-16" if b[:2] in (b"\xfe\xff", b"\xff\xfe") else "latin-1"
            return _clean(b.decode(enc, "ignore"))
        except Exception:
            pass
    return ""


def _from_pdf(path: str) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(min(size, 300 * 1024))
        if size > 300 * 1024:
            fh.seek(max(0, size - 300 * 1024))
            tail = fh.read(300 * 1024)
        else:
            tail = b""
    blob = head + tail
    title = _pdf_field(blob, "Title")
    author = _pdf_field(blob, "Author")
    year = _year(_pdf_field(blob, "CreationDate"))
    # XMP (Dublin Core) come ripiego per titolo/autore
    xmp = blob.decode("latin-1", "ignore")
    if not title:
        title = _xml_tag(xmp, "title")
    if not author:
        author = _xml_tag(xmp, "creator")
    if not year:
        year = _year(_xml_tag(xmp, "date"))
    return {"title": title, "author": author, "genre": "", "year": year}


def _from_rtf(path: str) -> dict:
    with open(path, "rb") as fh:
        blob = fh.read(200 * 1024).decode("latin-1", "ignore")
    def grp(name):
        m = re.search(r"\\%s\s+([^{}\\]+)" % name, blob)
        return _clean(m.group(1)) if m else ""
    return {"title": grp("title"), "author": grp("author"), "genre": "", "year": ""}


_EXTRACTORS = {
    "epub": _from_epub,
    "docx": _from_docx,
    "pdf": _from_pdf,
    "rtf": _from_rtf,
}


def extract(path: str, ext: str) -> dict:
    """Metadati best-effort {title, author, genre, year} (stringhe, '' se assenti)."""
    ext = (ext or "").lower()
    fn = _EXTRACTORS.get(ext)
    if not fn:
        return {"title": "", "author": "", "genre": "", "year": ""}
    try:
        m = fn(path)
    except Exception:
        m = {}
    return {
        "title": m.get("title", ""),
        "author": m.get("author", ""),
        "genre": m.get("genre", ""),
        "year": m.get("year", ""),
    }


# ─────────────────────────── nome del file ───────────────────────────
def _part(s: str, fallback: str, maxlen: int = 60) -> str:
    """Un segmento del nome: niente '-' interni (è il separatore) né char illegali."""
    s = _clean(s) or fallback
    s = s.replace("-", " ")                 # preserva il separatore a 4 campi
    s = _ILLEGAL.sub(" ", s)
    s = _WS.sub(" ", s).strip(" .")
    if len(s) > maxlen:
        s = s[:maxlen].strip()
    return s or fallback


def build_name(meta: dict, ext: str, fallback_stem: str) -> str:
    """Costruisce «Titolo-autore-genere-anno.ext» con segnaposto per i mancanti."""
    title = _part(meta.get("title", ""), fallback_stem or "senza titolo")
    author = _part(meta.get("author", ""), PH_AUTHOR)
    genre = _part(meta.get("genre", ""), PH_GENRE, maxlen=40)
    year = _part(meta.get("year", ""), PH_YEAR, maxlen=8)
    base = "-".join([title, author, genre, year])
    base = base[:180].strip(" .")
    return base + "." + ext.lower()
