"""Conversione di un documento in Markdown, IN MEMORIA e SENZA DIPENDENZE.

Perché in memoria
    Il .md non viene mai scritto su disco: nasce qui, viaggia insieme al file
    nel POST /api/agent/doc_upload (campo `md`) e muore lì. La cartella
    osservata dall'utente resta esattamente com'era — nessun file nuovo, nessun
    doppione, niente da ripulire.

Perché senza dipendenze
    L'agente viene distribuito come bundle .app: ogni libreria in più pesa sul
    pacchetto e sulla build CI. Qui si usa solo la libreria standard
    (zipfile, xml.etree, re, html): docx ed epub sono ZIP di XML, l'rtf è testo
    con parole di controllo, il txt è testo. Niente python-docx/ebooklib/striprtf.

Perché conviene farlo qui e non lasciarlo al server
    Il server estrae testo piatto. Qui abbiamo gli STILI del documento, quindi
    i titoli diventano `#`/`##`/`###` e gli elenchi `- `: l'Archivio riceve un
    Markdown strutturato, molto più utile per lettura, ricerca e AI. L'rtf, che
    il server oggi non legge affatto, qui viene almeno letto.

Formati
    docx  stili di paragrafo (Heading1/Titolo1…) → titoli, w:numPr → elenchi
    epub  ordine dello spine, un capitolo per file, h1-h6 → titoli
    rtf   parser minimo best-effort (escape \\'xx cp1252 e \\uNNNN)
    txt   lettura diretta con rilevamento codifica; il .md passa così com'è
    pdf   NON supportato qui: lo indicizza il server (o il browser dell'utente)
"""
import html
import logging
import os
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import unquote

log = logging.getLogger("hal_agent.docmd")

# Tetto prudente: oltre questa soglia il Markdown viene troncato, così non
# spediamo mostri da decine di MB dentro una POST multipart.
MAX_CHARS = 400_000
TRUNCATED = "\n\n…(testo troncato)"

# estensioni per cui sappiamo produrre Markdown
SUPPORTED = ("docx", "epub", "rtf", "txt", "md", "markdown", "text")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")
_BLANKS = re.compile(r"\n{3,}")


# ─────────────────────────── utilità comuni ────────────────────────────
def _decode(blob: bytes) -> str:
    """Testo da bytes provando le codifiche più probabili (utf-8 → cp1252 → latin-1).
    latin-1 non fallisce mai: è la rete di sicurezza."""
    if blob[:3] == b"\xef\xbb\xbf":
        blob = blob[3:]
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return blob.decode("latin-1", "ignore")


def _inline(s: str) -> str:
    """Contenuto di una riga: via i tag residui, spazi collassati, niente a capo."""
    s = _TAG.sub(" ", s or "")
    s = s.replace("\n", " ").replace("\r", " ")
    return _WS.sub(" ", s).strip()


def _normalize(md: str) -> str:
    """A capo uniformi, code di spazi via, mai più di due righe vuote di fila,
    e il tetto di MAX_CHARS."""
    md = (md or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    md = "\n".join(line.rstrip() for line in md.split("\n"))
    md = _BLANKS.sub("\n\n", md).strip()
    if len(md) > MAX_CHARS:
        md = md[:MAX_CHARS].rstrip() + TRUNCATED
    return md


# ─────────────────────────────── docx ──────────────────────────────────
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Heading1 / Titolo1 / Überschrift1 / Titre1 …: la cifra finale è il livello
_HEAD_RE = re.compile(r"^(?:heading|titolo|titre|title|berschrift|encabezado|t[ií]tulo|kop)(\d)$")
_TITLE_STYLES = {"title", "titolo", "titel", "titre"}
_SUBTITLE_STYLES = {"subtitle", "sottotitolo", "untertitel", "soustitre"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _docx_run_text(el) -> str:
    """Testo di un paragrafo: concatena i <w:t>, traduce tab e interruzioni,
    e salta il testo cancellato con revisioni attive (<w:del>)."""
    out = []
    for child in el:
        tag = _local(child.tag)
        if tag == "t":
            out.append(child.text or "")
        elif tag == "tab":
            out.append(" ")
        elif tag in ("br", "cr"):
            out.append("\n")
        elif tag in ("del", "delText", "instrText"):
            continue
        elif len(child):
            out.append(_docx_run_text(child))
    return "".join(out)


def _docx_level(style: str, outline: str) -> int:
    """Livello del titolo (1-6) dallo stile di paragrafo; 0 = testo normale.
    Se lo stile non dice nulla si guarda w:outlineLvl (0 = titolo di primo livello)."""
    s = (style or "").strip().lower()
    for ch in (" ", "-", "_", "."):
        s = s.replace(ch, "")
    s = s.replace("ü", "").replace("ö", "")     # Überschrift1 → berschrift1
    m = _HEAD_RE.match(s)
    if m:
        return max(1, min(6, int(m.group(1))))
    if s in _TITLE_STYLES:
        return 1
    if s in _SUBTITLE_STYLES:
        return 2
    if outline not in (None, ""):
        try:
            lvl = int(outline) + 1
        except ValueError:
            return 0
        if 1 <= lvl <= 6:
            return lvl
    return 0


def _docx_md(path: str) -> str:
    """Markdown da un .docx: è uno ZIP, il testo sta in word/document.xml."""
    with zipfile.ZipFile(path) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError:
            # qualche generatore mette il documento altrove
            names = [n for n in z.namelist()
                     if n.lower().endswith("document.xml") and n.lower().startswith("word/")]
            if not names:
                return ""
            xml = z.read(names[0])
    root = ET.fromstring(xml)

    lines = []
    for p in root.iter(_W + "p"):
        style = outline = ""
        numbered = False
        ilvl = 0
        ppr = p.find(_W + "pPr")
        if ppr is not None:
            el = ppr.find(_W + "pStyle")
            if el is not None:
                style = el.get(_W + "val") or ""
            el = ppr.find(_W + "outlineLvl")
            if el is not None:
                outline = el.get(_W + "val") or ""
            npr = ppr.find(_W + "numPr")
            if npr is not None:
                numbered = True
                el = npr.find(_W + "ilvl")
                if el is not None:
                    try:
                        ilvl = max(0, min(5, int(el.get(_W + "val") or 0)))
                    except ValueError:
                        ilvl = 0

        text = _inline(_docx_run_text(p))
        if not text:
            continue
        lvl = _docx_level(style, outline)
        if lvl:
            lines.append("\n" + "#" * lvl + " " + text + "\n")
        elif numbered:
            lines.append("  " * ilvl + "- " + text)
        else:
            lines.append("\n" + text + "\n")
    return "\n".join(lines)


# ─────────────────────────────── epub ──────────────────────────────────
def _epub_opf_name(z: zipfile.ZipFile) -> str:
    """Percorso dell'OPF dichiarato in META-INF/container.xml (con ripiego)."""
    try:
        cont = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', cont)
        if m:
            return m.group(1)
    except KeyError:
        pass
    names = [n for n in z.namelist() if n.lower().endswith(".opf")]
    return names[0] if names else ""


def _epub_spine(opf: str):
    """(id → href) del manifest e ordine di lettura dello spine."""
    manifest, spine = {}, []
    try:
        root = ET.fromstring(opf.encode("utf-8", "ignore"))
    except ET.ParseError:
        # OPF malformato: ripiego a regex, meglio poco che niente
        for m in re.finditer(r'<item\b[^>]*>', opf, re.I):
            tag = m.group(0)
            i = re.search(r'\bid="([^"]+)"', tag)
            h = re.search(r'\bhref="([^"]+)"', tag)
            if i and h:
                manifest[i.group(1)] = h.group(1)
        spine = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf, re.I)
        return manifest, spine
    for el in root.iter():
        tag = _local(el.tag).lower()
        if tag == "item" and el.get("id") and el.get("href"):
            manifest[el.get("id")] = el.get("href")
        elif tag == "itemref" and el.get("idref"):
            spine.append(el.get("idref"))
    return manifest, spine


def _html_md(doc: str) -> str:
    """XHTML di un capitolo → Markdown: h1-h6 → #…######, li → «- », i blocchi
    diventano paragrafi, il resto dei tag sparisce."""
    s = re.sub(r"(?is)<!--.*?-->", " ", doc)
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", s)
    if m:
        title = _inline(m.group(1))
    s = re.sub(r"(?is)<(script|style|head)\b[^>]*>.*?</\1>", " ", s)

    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    has_head = False
    for lvl in range(1, 7):
        pat = r"(?is)<h%d\b[^>]*>(.*?)</h%d>" % (lvl, lvl)
        if re.search(pat, s):
            has_head = True
        s = re.sub(pat, lambda m, l=lvl: "\n\n" + "#" * l + " " + _inline(m.group(1)) + "\n\n", s)
    # niente riga vuota fra un <li> e l'altro: l'elenco resta compatto
    s = re.sub(r"(?is)<li\b[^>]*>(.*?)</li>", lambda m: "\n- " + _inline(m.group(1)), s)
    s = re.sub(r"(?i)<(?:p|div|section|article|tr|blockquote|pre|table|ul|ol|hr)\b[^>]*>", "\n\n", s)
    s = re.sub(r"(?i)</(?:p|div|section|article|tr|blockquote|pre|table|ul|ol)>", "\n\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)

    lines = [_WS.sub(" ", ln).strip() for ln in s.replace("\r", "\n").split("\n")]
    s = "\n".join(lines).strip()
    if title and not has_head and s:
        s = "# " + title + "\n\n" + s
    return s


def _epub_md(path: str) -> str:
    """Markdown da un .epub: i capitoli nell'ordine dello spine, uno per file."""
    with zipfile.ZipFile(path) as z:
        opf_name = _epub_opf_name(z)
        if not opf_name:
            return ""
        opf = z.read(opf_name).decode("utf-8", "ignore")
        base = posixpath.dirname(opf_name)
        manifest, spine = _epub_spine(opf)
        names = set(z.namelist())

        parts = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            href = unquote(href.split("#", 1)[0])
            name = posixpath.normpath(posixpath.join(base, href)) if base else href
            if name not in names:
                continue
            if not re.search(r"\.(x?html?|xml)$", name, re.I):
                continue
            try:
                chunk = _html_md(_decode(z.read(name)))
            except Exception as e:                       # capitolo illeggibile: si prosegue
                log.debug("epub: capitolo %s saltato (%s)", name, e)
                continue
            if chunk:
                parts.append(chunk)
    return "\n\n".join(parts)


# ──────────────────────────────── rtf ──────────────────────────────────
# gruppi da buttare via in blocco: tabelle di font/colori/stili, metadati, immagini
_RTF_SKIP = re.compile(
    r"\\(?:\*|(?:fonttbl|colortbl|stylesheet|info|pict|object|themedata|generator"
    r"|colorschememapping|latentstyles|listtable|listoverridetable|rsidtbl|filetbl"
    r"|xmlnstbl|datastore|upr|header|headerl|headerr|headerf|footer|footerl|footerr"
    r"|footerf|fldinst)\b)")
_RTF_ESC = {"\\": "\x00b", "{": "\x00o", "}": "\x00c"}


def _rtf_drop_groups(rtf: str) -> str:
    """Elimina i gruppi «di servizio» (\\*\\… , fonttbl, stylesheet, pict…),
    tenendo conto dell'annidamento delle graffe."""
    out = []
    depth = 0
    skip_at = None
    i, n = 0, len(rtf)
    while i < n:
        ch = rtf[i]
        if ch == "\\" and i + 1 < n and rtf[i + 1] in "{}\\":
            if skip_at is None:
                out.append(rtf[i:i + 2])
            i += 2
            continue
        if ch == "{":
            depth += 1
            if skip_at is None:
                if _RTF_SKIP.match(rtf, i + 1):
                    skip_at = depth
                else:
                    out.append(ch)
            i += 1
            continue
        if ch == "}":
            if skip_at is not None and depth == skip_at:
                skip_at = None
            elif skip_at is None:
                out.append(ch)
            depth = max(0, depth - 1)
            i += 1
            continue
        if skip_at is None:
            out.append(ch)
        i += 1
    return "".join(out)


def _rtf_md(path: str) -> str:
    """Testo da un .rtf: parser minimo (best-effort) con la sola libreria standard.
    L'rtf non ha una struttura di titoli affidabile, quindi il risultato è testo
    a paragrafi — che è comunque molto più di quanto HAL legga oggi (nulla)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    s = raw.decode("latin-1", "ignore")          # l'rtf è ASCII con escape propri
    s = _rtf_drop_groups(s)

    # caratteri protetti: \\ \{ \} non devono sparire con le graffe di struttura
    for k, v in _RTF_ESC.items():
        s = s.replace("\\" + k, v)

    # escape esadecimali \'e8 → è (tabella cp1252, la più comune)
    s = re.sub(r"\\'([0-9a-fA-F]{2})",
               lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), s)
    # unicode \u232? (il carattere di ripiego dopo il numero va scartato)
    s = re.sub(r"\\u(-?\d+)\s?\??",
               lambda m: chr(int(m.group(1)) % 65536) if m.group(1) else "", s)
    # a capo
    s = re.sub(r"\\(?:par|line|sect|page)\b[ ]?", "\n", s)
    s = re.sub(r"\\tab\b[ ]?", " ", s)
    # parole di controllo residue (\b3, \fs24, \pard…) e simboli di controllo
    s = re.sub(r"\\[a-zA-Z]+-?\d*[ ]?", "", s)
    s = re.sub(r"\\[^a-zA-Z]", "", s)
    s = s.replace("{", "").replace("}", "")

    for k, v in _RTF_ESC.items():
        s = s.replace(v, k)
    lines = [_WS.sub(" ", ln).strip() for ln in s.replace("\r", "\n").split("\n")]
    return "\n".join(lines)


# ──────────────────────────── txt / md ─────────────────────────────────
def _text_md(path: str, ext: str) -> str:
    """Testo semplice. Il .md passa così com'è (è già Markdown: non lo tocchiamo),
    il .txt viene solo decodificato."""
    with open(path, "rb") as fh:
        raw = fh.read(MAX_CHARS * 4)
    return _decode(raw)


# ────────────────────────────── ingresso ───────────────────────────────
def convert_to_markdown(path: str) -> tuple:
    """(markdown, motivo_se_vuoto).

    Ritorna sempre una coppia: se il Markdown è vuoto il secondo elemento dice
    perché (formato non gestito, file illeggibile, documento senza testo).
    Non solleva eccezioni: chi chiama deve poter caricare il file comunque.
    """
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "pdf":
        return "", "pdf: la conversione la fa il server"
    if ext not in SUPPORTED:
        return "", f"{ext or 'senza estensione'}: conversione non supportata dall'agente"

    try:
        if ext == "docx":
            md = _docx_md(path)
        elif ext == "epub":
            md = _epub_md(path)
        elif ext == "rtf":
            md = _rtf_md(path)
        else:
            md = _text_md(path, ext)
    except Exception as e:
        log.debug("conversione fallita per %s: %s", path, e)
        return "", f"conversione fallita: {str(e)[:120]}"

    if ext in ("md", "markdown"):
        # già Markdown: nessuna normalizzazione, solo il tetto di lunghezza
        md = md.strip()
        if len(md) > MAX_CHARS:
            md = md[:MAX_CHARS].rstrip() + TRUNCATED
    else:
        md = _normalize(md)
    if not md:
        return "", "nessun testo estratto"
    return md, ""
