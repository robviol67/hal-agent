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

I file NON vengono modificati né spostati: si può continuare ad aggiungere
righe. Il dedup sta nello stato locale (ID già mandati) e nel server (url_hash).
"""
import logging
import os

import httpx

from . import config as cfg
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
            if os.path.splitext(name)[1].lower().lstrip(".") in exts:
                yield os.path.join(root, name)


def _read_ids(path: str) -> list:
    """ID video (unici, in ordine) trovati nel file; righe # = commenti."""
    ids, seen = [], set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                for tok in s.replace(",", " ").split():
                    vid = transcripts.extract_video_id(tok)
                    if vid and vid not in seen:
                        seen.add(vid)
                        ids.append(vid)
    except OSError as e:
        log.debug("file non leggibile %s: %s", path, e)
    return ids


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


def scan_once(conf: dict = None, on_progress=None, dry_run: bool = False) -> dict:
    """Un giro: legge gli elenchi, trascrive i video nuovi e li manda al Feed."""
    conf = conf or cfg.load_config()
    vc = conf.get("video", {}) or {}

    def prog(m):
        if on_progress:
            on_progress(m)

    result = {"files": 0, "links": 0, "new": 0, "sent": 0, "done": 0, "none": 0, "error": 0,
              "fallback": 0, "folder": vc.get("folder", ""), "ok": True}

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

    prog(f"Video: lettura di {folder}…")
    ids = []
    for path in _iter_files(folder, exts):
        result["files"] += 1
        for vid in _read_ids(path):
            result["links"] += 1
            if vid not in sent_ids and vid not in ids:
                ids.append(vid)
    result["new"] = len(ids)
    if not ids:
        prog(f"Video: nessun link nuovo ({result['links']} in {result['files']} file)")
        return result
    todo = ids[:max_per_run]

    items = []
    for i, vid in enumerate(todo):
        prog(f"Video: {i + 1}/{len(todo)} — {vid}")
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
            "_vid": vid,
        })
        if dry_run:
            print(f"{vid}  {title[:60]}  →  {t['status']}"
                  + (f" ({len(t['text'])} caratteri, {t['lang']})" if t["status"] == "done" else f" ({t['detail']})"))

    if dry_run:
        prog(f"Video (dry-run): {len(todo)} nuovi, {result['done']} con sottotitoli, {result['none']} senza")
        return result

    payload = [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]
    res = sender.send(payload, conf)
    if res.get("ok"):
        # Segna come mandati anche i video con errore momentaneo: sul server sono
        # in coda 'pending' e li riprende il giro delle trascrizioni, non questo.
        for it in items:
            sent_ids.add(it["_vid"])
        result["sent"] = len(items)
        vstate["sent_ids"] = list(sent_ids)[-20000:]
        cfg.save_state(state)
        if result["none"] > 0:
            result["fallback"] = transcripts.run_fallback(conf, on_progress=prog)
    else:
        result["ok"] = False
        result["error_msg"] = res.get("error", "invio fallito")

    msg = (f"Video: {result['sent']} mandati al Feed, {result['done']} con sottotitoli, "
           f"{result['none']} senza (Gemini: {result['fallback']})")
    if result["error"]:
        msg += f", {result['error']} da ritentare"
    prog(msg)
    log.info("Video: file=%d link=%d nuovi=%d inviati=%d done=%d none=%d err=%d",
             result["files"], result["links"], result["new"], result["sent"],
             result["done"], result["none"], result["error"])
    return result
