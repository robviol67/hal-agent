"""Capability «Video»: osserva una cartella di file di testo con link YouTube e
manda ogni video nuovo al Feed di HAL, già trascritto.

È la terza cartella osservata (dopo Libreria e Documenti), ma qui dentro non ci
vanno file da caricare: ci vanno ELENCHI DI LINK. Un file .txt (o .md) per
riga un link YouTube — watch, youtu.be, shorts, o anche l'ID nudo. Le righe che
iniziano con # sono commenti. Esempio (~/HAL/Video/da-vedere.txt):

    # conferenza di martedì
    https://www.youtube.com/watch?v=dQw4w9WgXcQ
    https://youtu.be/kJQP7kiw5Fk

Per ogni link nuovo l'agente:
  1. chiede a YouTube titolo e canale (oEmbed, senza chiave);
  2. scarica i sottotitoli (transcripts.get_transcript);
  3. lo manda all'ingest come elemento del Feed (source youtube, Scout
     «Cartella video»), con la trascrizione allegata. Se i sottotitoli non
     esistono, lo dice al server, che passa il video a Gemini.

SOLO TESTO — di certi video interessa la trascrizione, non la parte visiva.
Si marcano nell'elenco in tre modi (l'uno vince sull'altro, in quest'ordine):

  1. CARTELLA   ~/HAL/Video/Solo testo/…      tutti i link lì dentro
  2. FILE       `#! testo` in una riga         tutto il file (ovunque si trovi)
  3. RIGA       `[testo]` in fondo alla riga   solo quel link

Dopo la freccia si può indicare il PROGETTO di destinazione, e il progetto si
può chiedere anche per un video normale:

    #! testo → Ricerca AI
    https://youtu.be/AAAAAAAAAAA               (solo testo, progetto Ricerca AI)
    https://youtu.be/BBBBBBBBBBB  [video]      (eccezione: questo si guarda)
    https://youtu.be/CCCCCCCCCCC  [testo → Frontiera]
    https://youtu.be/DDDDDDDDDDD  [→ Ricerca AI]   (resta un video, ma nel progetto)

HAL riceve il marcatore e, appena la trascrizione è pronta, archivia da solo il
testo in «Leggi» dentro quel progetto (server: lib/videotext.php).

I file NON vengono modificati né spostati: si può continuare ad aggiungere
righe. Il dedup sta nello stato locale (ID già mandati, con la loro modalità:
cambiare marcatore a un link già mandato lo rimanda) e nel server (url_hash).
"""
import logging
import os
import re
import time

import httpx

from . import config as cfg
from . import playlists
from . import sender
from . import transcripts

log = logging.getLogger("hal_agent.videos")

_SKIP_PREFIX = (".", "~$")
_SKIP_SUFFIX = (".crdownload", ".part", ".tmp", ".download")


def _iter_files(folder: str, exts: set):
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith(_SKIP_PREFIX) or name.lower().endswith(_SKIP_SUFFIX):
                continue
            if name == README_NAME:          # il promemoria non è un elenco
                continue
            if os.path.splitext(name)[1].lower().lstrip(".") in exts:
                yield os.path.join(root, name)


# --- marcatori «solo testo» / progetto -------------------------------------
# Parole che dicono «di questo interessa il testo» e parole che dicono il contrario.
_TEXT_WORDS = {"testo", "solo testo", "soltanto testo", "trascrizione", "solo trascrizione",
               "trascrizioni", "leggi", "text"}
_VIDEO_WORDS = {"video", "guarda", "da guardare", "ascolta", "normale"}
_PROJ_WORDS = {"progetto", "project", "studio"}
# freccia fra la modalità e il nome del progetto: «testo → Ricerca AI»
_ARROW = re.compile(r"\s*(?:→|->|=>|»|·|:|>)\s*")
# marcatore di riga: [testo], [testo → Progetto], [→ Progetto], [video]
_MARK = re.compile(r"\[([^\[\]]{1,160})\]")


def _norm(s: str) -> str:
    """Minuscole, separatori appianati: «Solo-Testo» e «solo testo» sono la stessa cosa."""
    s = (s or "").strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(s.split())


def _parse_spec(spec: str, strict: bool = False):
    """
    Legge un marcatore e ritorna (text_only, project), entrambi None se non dice niente.
    `strict` (nomi di cartella): una parola sconosciuta NON diventa un progetto,
    altrimenti qualunque sottocartella finirebbe per battezzare un progetto.
    """
    spec = (spec or "").strip()
    if not spec:
        return None, None
    parts = _ARROW.split(spec, 1)
    head = _norm(parts[0])
    tail = parts[1].strip() if len(parts) > 1 else ""

    if head in _TEXT_WORDS:
        return True, (tail or None)
    if head in _VIDEO_WORDS:
        return False, (tail or None)
    if head in _PROJ_WORDS:
        return None, (tail or None)
    if head == "":                      # «[→ Ricerca AI]»: solo il progetto
        return None, (tail or None)
    if strict:
        return None, None
    # testa sconosciuta: è il nome di un progetto — «[Ricerca AI]»
    return None, (spec or None)


def _folder_spec(folder: str, path: str):
    """Modalità che arriva dal NOME DELLE SOTTOCARTELLE sotto la cartella osservata."""
    text_only, project = None, None
    try:
        rel = os.path.relpath(os.path.dirname(path), folder)
    except ValueError:
        return None, None
    if rel in (".", os.curdir, ""):
        return None, None
    for seg in rel.split(os.sep):
        t, p = _parse_spec(seg, strict=True)
        if t is not None:
            text_only = t
        if p:
            project = p
    return text_only, project


def _read_entries(path: str, text_only=None, project=None) -> list:
    """
    Link trovati nel file, in ordine e senza ripetizioni, ciascuno con la sua
    modalità: [{'vid', 'text_only', 'project'}]. Righe `#` = commenti, righe
    `#!` = direttive che valgono per TUTTO il file (anche se stanno in fondo).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        log.debug("file non leggibile %s: %s", path, e)
        return []

    # 1° passaggio: le direttive del file (valgono ovunque siano scritte)
    for line in lines:
        t = line.strip()
        if t.startswith("#!"):
            ft, fp = _parse_spec(t[2:])
            if ft is not None:
                text_only = ft
            if fp:
                project = fp

    # 2° passaggio: i link, con l'eventuale marcatore di riga
    out, seen = [], set()
    for line in lines:
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        lt, lp = text_only, project
        m = _MARK.search(t)
        if m:
            mt, mp = _parse_spec(m.group(1))
            if mt is not None:
                lt = mt
            if mp:
                lp = mp
            t = (t[:m.start()] + " " + t[m.end():]).strip()
        for tok in t.replace(",", " ").split():
            vid = transcripts.extract_video_id(tok)
            if vid:
                if vid not in seen:
                    seen.add(vid)
                    out.append({"vid": vid, "playlist": "", "text_only": bool(lt), "project": (lp or "")})
                continue
            # indirizzo di una PLAYLIST (o id nudo): vale per tutti i video che contiene.
            # Un link «watch?v=…&list=…» resta un video solo: c'è il video, si prende quello.
            plid = playlists.extract_playlist_id(tok)
            if plid and plid not in seen:
                seen.add(plid)
                out.append({"vid": "", "playlist": plid, "text_only": bool(lt), "project": (lp or "")})
    return out


README_NAME = "COME SI SCRIVE.txt"
README_TEXT = """\
COME SI SCRIVE UN ELENCO — cartella video di HAL
================================================

In questa cartella NON vanno i video: vanno file di testo (.txt) con
UN LINK YOUTUBE PER RIGA. Va bene qualunque forma del link, e va bene
anche l'indirizzo di una PLAYLIST intera.

    # le righe che iniziano con # sono commenti
    https://www.youtube.com/watch?v=XXXXXXXXXXX
    https://youtu.be/XXXXXXXXXXX
    https://youtube.com/playlist?list=PL...      <- tutta la playlist

DI QUESTO VOGLIO SOLO IL TESTO
------------------------------
Se di un video non ti interessa guardarlo ma leggerlo, marcalo: appena la
trascrizione è pronta HAL la archivia da sola in «Leggi», come documento.
Tre modi, dal più generale al più preciso (l'ultimo vince):

  1. CARTELLA   metti il file in una sottocartella «Solo testo»
  2. FILE       scrivi una riga     #! testo
  3. RIGA       aggiungi in fondo   [testo]

IL PROGETTO
-----------
Dopo la freccia indichi dove finisce il testo. Se il progetto non esiste,
viene creato.

    #! testo -> Ricerca AI
    https://youtu.be/XXXXXXXXXXX                 (solo testo, in «Ricerca AI»)
    https://youtu.be/XXXXXXXXXXX  [video]        (eccezione: questo si guarda)
    https://youtu.be/XXXXXXXXXXX  [testo -> Frontiera]
    https://youtu.be/XXXXXXXXXXX  [-> Ricerca AI]  (resta un video, ma nel progetto)

COSE DA SAPERE
--------------
- I file non vengono mai modificati né spostati: aggiungi righe quando vuoi.
- Un link già mandato non viene rimandato; se però gli cambi marcatore, sì.
- Togliere una riga non cancella niente: quello che è arrivato resta.
- Le playlist restano sotto osservazione: i video aggiunti dopo arrivano da soli.
- Un link «watch?v=...&list=...» è UN video solo: per prendere tutta la
  playlist serve l'indirizzo che comincia con «playlist?list=».

NON TI VA DI SCRIVERE FILE?
---------------------------
Sul sito c'è «Aggiungi video»: incolli link, playlist o un canale, scegli
dove mandarli e in quale progetto, e non c'è nessuna sintassi da ricordare.
Funziona anche dal telefono.
"""


def _ensure_readme(folder: str, vstate) -> None:
    """Mette il promemoria nella cartella, una volta sola: la sintassi sta dove serve."""
    if vstate is None:
        return
    if vstate.get("readme_written"):
        return
    path = os.path.join(folder, README_NAME)
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(README_TEXT)
        vstate["readme_written"] = True
    except OSError as e:
        log.debug("promemoria non scritto: %s", e)


def _signature(e: dict) -> str:
    """Come è stato mandato un link: cambiando marcatore cambia la firma e si rimanda."""
    return ("T" if e["text_only"] else "V") + "|" + (e["project"] or "")


def _playlist_ids(plid: str, vc: dict, vstate: dict, refresh: bool, prog) -> dict:
    """
    Video di una playlist, con una cache nello stato: la playlist si rilegge ogni
    playlist_refresh_minutes (o subito, se il giro è stato chiesto a mano), così i
    video aggiunti dopo arrivano da soli senza bussare a YouTube ogni cinque minuti.
    """
    cache = vstate.setdefault("playlists", {}) if vstate is not None else {}
    cached = cache.get(plid) or {}
    age_ok = (time.time() - float(cached.get("at", 0))) < int(vc.get("playlist_refresh_minutes", 60) or 60) * 60
    if cached.get("ids") and age_ok and not refresh:
        return {"ids": list(cached["ids"]), "title": cached.get("title", ""), "error": "", "cached": True}

    limit = int(vc.get("playlist_max", 200) or 200)
    prog(f"Playlist: lettura di {plid}…")
    r = playlists.playlist_videos(plid, limit=limit)
    if not r["ok"]:
        if cached.get("ids"):      # YouTube non risponde: si va avanti con l'ultimo elenco buono
            log.warning("Playlist %s non letta (%s): uso l'elenco in cache", plid, r["error"])
            return {"ids": list(cached["ids"]), "title": cached.get("title", ""), "error": r["error"], "cached": True}
        return {"ids": [], "title": "", "error": r["error"], "cached": False}

    title = r["title"] or plid
    prog(f"Playlist «{title}»: {len(r['ids'])} video" + (" (tetto raggiunto)" if r["partial"] else ""))
    if vstate is not None:
        cache[plid] = {"at": time.time(), "ids": r["ids"], "title": r["title"]}
    return {"ids": r["ids"], "title": r["title"], "error": "", "cached": False}


def _oembed(video_id: str) -> dict:
    """Titolo e canale senza chiave né quota. {'title','channel','ok'}."""
    try:
        r = httpx.get("https://www.youtube.com/oembed",
                      params={"url": transcripts.watch_url(video_id), "format": "json"},
                      timeout=10, follow_redirects=True)
        if r.status_code == 200:
            d = r.json() or {}
            return {"ok": True, "title": str(d.get("title") or "").strip(),
                    "channel": str(d.get("author_name") or "").strip()}
        return {"ok": False, "title": "", "channel": "", "http": r.status_code}
    except Exception as e:
        log.debug("oEmbed %s fallito: %s", video_id, e)
        return {"ok": False, "title": "", "channel": ""}


def scan_once(conf: dict = None, on_progress=None, dry_run: bool = False,
              refresh_playlists: bool = False) -> dict:
    """
    Un giro: legge gli elenchi, apre le playlist, trascrive i video nuovi e li
    manda al Feed. `refresh_playlists` salta la cache delle playlist (è quello
    che succede quando il giro lo chiedi tu dal menu).
    """
    conf = conf or cfg.load_config()
    vc = conf.get("video", {}) or {}

    def prog(m):
        if on_progress:
            on_progress(m)

    result = {"files": 0, "links": 0, "new": 0, "text": 0, "sent": 0, "done": 0, "none": 0,
              "error": 0, "fallback": 0, "playlists": 0, "playlist_err": 0,
              "folder": vc.get("folder", ""), "ok": True}

    folder = vc.get("folder") or ""
    if not folder or not os.path.isdir(folder):
        result.update(ok=False, error_msg="cartella inesistente")
        prog(f"Video: cartella non trovata ({folder or '—'})")
        return result
    if not conf.get("token") and not dry_run:
        result.update(ok=False, error_msg="token mancante")
        prog("Video: token mancante (configura il collegamento)")
        return result

    exts = set(vc.get("exts") or ["txt", "md", "urls"])
    agent_name = str(vc.get("agent_name") or "Cartella video")
    max_per_run = int(vc.get("max_per_run", 20) or 20)

    state = {} if dry_run else cfg.load_state()
    vstate = state.setdefault("video", {}) if not dry_run else {}
    sent_ids = set(vstate.get("sent_ids", [])) if not dry_run else set()
    # firma di come ogni link è stato mandato (modalità + progetto): i link mandati
    # prima di questa versione non ce l'hanno e valgono come video normali, così
    # l'aggiornamento dell'agente non rimanda tutto da capo.
    sent_modes = dict(vstate.get("sent_modes", {})) if not dry_run else {}

    prog(f"Video: lettura di {folder}…")
    if not dry_run:
        _ensure_readme(folder, vstate)
    raw = []
    for path in _iter_files(folder, exts):
        result["files"] += 1
        ft, fp = _folder_spec(folder, path)
        raw.extend(_read_entries(path, ft, fp))

    # Le playlist diventano i loro video, che ereditano il marcatore della riga.
    entries, pl_done, pl_fetched = [], set(), False
    for e in raw:
        plid = e.get("playlist") or ""
        if not plid:
            entries.append(e)
            continue
        if plid in pl_done:
            continue
        pl_done.add(plid)
        result["playlists"] += 1
        r = _playlist_ids(plid, vc, None if dry_run else vstate, refresh_playlists, prog)
        if r["error"] and not r["ids"]:
            result["playlist_err"] += 1
            prog(f"Playlist {plid}: {r['error']}")
            continue
        if not r["cached"]:
            pl_fetched = True
        for vid in r["ids"]:
            entries.append({"vid": vid, "playlist": plid, "text_only": e["text_only"],
                            "project": e["project"]})

    # la cache delle playlist si salva subito: vale anche se poi non c'è niente da mandare
    if pl_fetched and not dry_run:
        cfg.save_state(state)

    todo_all, seen = [], set()
    for e in entries:
        result["links"] += 1
        vid = e["vid"]
        if not vid or vid in seen:
            continue
        already = vid in sent_ids and sent_modes.get(vid, "V|") == _signature(e)
        if not already:
            seen.add(vid)
            todo_all.append(e)
    result["new"] = len(todo_all)
    result["text"] = sum(1 for e in todo_all if e["text_only"])
    if not todo_all:
        prog(f"Video: nessun link nuovo ({result['links']} in {result['files']} file"
             + (f", {result['playlists']} playlist" if result["playlists"] else "") + ")")
        return result
    todo = todo_all[:max_per_run]

    items = []
    for i, e in enumerate(todo):
        vid = e["vid"]
        prog(f"Video: {i + 1}/{len(todo)} — {vid}" + (" (solo testo)" if e["text_only"] else ""))
        meta = _oembed(vid)
        title = meta["title"] or ("Video " + vid)
        t = transcripts.get_transcript(vid)
        result[t["status"]] = result.get(t["status"], 0) + 1
        excerpt = (t["text"][:500] + "…") if len(t["text"]) > 500 else t["text"]
        items.append({
            "title": title,
            "excerpt": excerpt,
            "url": transcripts.watch_url(vid),
            "source": "youtube",
            "published": "",
            "channel": meta["channel"],
            "author": "",
            "agent": agent_name,
            "transcript": t["text"],
            "transcript_status": t["status"],
            "transcript_detail": t["detail"],
            # marcatori dell'elenco: HAL archivia da solo il testo in «Leggi»
            "text_only": bool(e["text_only"]),
            "project": e["project"],
            "_vid": vid,
            "_sig": _signature(e),
        })
        if dry_run:
            mode = "solo testo" if e["text_only"] else "video"
            if e["project"]:
                mode += f" → {e['project']}"
            print(f"{vid}  {title[:50]}  [{mode}]  →  {t['status']}"
                  + (f" ({len(t['text'])} caratteri, {t['lang']})" if t["status"] == "done" else f" ({t['detail']})"))

    if dry_run:
        prog(f"Video (dry-run): {len(todo)} nuovi ({result['text']} solo testo), "
             f"{result['done']} con sottotitoli, {result['none']} senza")
        return result

    payload = [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]
    res = sender.send(payload, conf)
    if res.get("ok"):
        # Segna come mandati anche i video con errore momentaneo: sul server sono
        # in coda 'pending' e li riprende il giro delle trascrizioni, non questo.
        for it in items:
            sent_ids.add(it["_vid"])
            sent_modes[it["_vid"]] = it["_sig"]
        result["sent"] = len(items)
        keep = list(sent_ids)[-20000:]
        vstate["sent_ids"] = keep
        vstate["sent_modes"] = {v: sent_modes[v] for v in keep if v in sent_modes}
        cfg.save_state(state)
        if result["none"] > 0:
            result["fallback"] = transcripts.run_fallback(conf, on_progress=prog)
    else:
        result["ok"] = False
        result["error_msg"] = res.get("error", "invio fallito")

    msg = (f"Video: {result['sent']} mandati al Feed"
           + (f" ({result['text']} solo testo)" if result["text"] else "")
           + f", {result['done']} con sottotitoli, {result['none']} senza (Gemini: {result['fallback']})")
    if result["error"]:
        msg += f", {result['error']} da ritentare"
    prog(msg)
    log.info("Video: file=%d link=%d nuovi=%d (solo testo=%d) inviati=%d done=%d none=%d err=%d",
             result["files"], result["links"], result["new"], result["text"], result["sent"],
             result["done"], result["none"], result["error"])
    return result
