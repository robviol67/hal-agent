"""Trascrizioni dei video YouTube.

Due lavori, entrambi dall'IP di casa (YouTube blocca i datacenter):
  1. `get_transcript(video_id)` scarica i sottotitoli che YouTube ha già
     (manuali o automatici): gratis, in un secondo, senza chiave. Lo usano lo
     Scout (fetcher.py) e la cartella dei video (videos.py).
  2. `poll_and_run_once(conf)` svuota la coda del sito (/api/agent/transcribe):
     i video per cui la trascrizione è stata chiesta a mano dal Feed, o quelli
     che lo Scout non era riuscito a scaricare. Poi chiede al server di eseguire
     il FALLBACK Gemini per i video senza sottotitoli (`run_fallback`): il lavoro
     lo fa il server, qui si fa solo da "sveglia", un video per richiesta.
"""
import logging
import time
import re

import httpx

from . import config as cfg
from . import telemetry

log = logging.getLogger("hal_agent.transcripts")

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api import _errors as _yte
    YT_AVAILABLE = True
except Exception:          # libreria assente: si va avanti senza trascrizioni
    YouTubeTranscriptApi = None
    _yte = None
    YT_AVAILABLE = False

# lingue preferite, in ordine; se nessuna c'è si prende la prima disponibile
PREFERRED_LANGS = ("it", "it-IT", "en", "en-US", "en-GB")
_YT_ID = re.compile(r"(?:v=|youtu\.be/|embed/|shorts/|live/)([A-Za-z0-9_-]{11})")


def extract_video_id(s: str):
    """ID video da un URL YouTube (watch, youtu.be, embed, shorts, live) o da un ID nudo."""
    s = (s or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = _YT_ID.search(s)
    return m.group(1) if m else None


def watch_url(video_id: str) -> str:
    return "https://www.youtube.com/watch?v=" + video_id


def _err_names(exc) -> str:
    return type(exc).__name__


def _is_none_error(exc) -> bool:
    """Errori "definitivi": il video non ha sottotitoli o non è accessibile."""
    return _err_names(exc) in {
        "TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable",
        "VideoUnplayable", "AgeRestricted", "NotTranslatable", "InvalidVideoId",
    }


def _is_block_error(exc) -> bool:
    """Errori transitori: YouTube ci ha bloccato per un po' (si ritenta più tardi)."""
    return _err_names(exc) in {"IpBlocked", "RequestBlocked", "TooManyRequests", "YouTubeRequestFailed",
                               "YouTubeDataUnparsable", "PoTokenRequired"}


def _snippets(fetched):
    """Normalizza il risultato delle due generazioni della libreria in [(start, text), …]."""
    out = []
    for p in fetched:
        if isinstance(p, dict):
            out.append((float(p.get("start", 0) or 0), str(p.get("text", "") or "")))
        else:
            out.append((float(getattr(p, "start", 0) or 0), str(getattr(p, "text", "") or "")))
    return out


def _join(snippets, paragraph_every: float = 75.0) -> str:
    """Unisce i frammenti in testo: spazi tra i frammenti, una riga vuota ogni
    ~75 secondi di parlato, così le trascrizioni automatiche (senza punteggiatura)
    restano leggibili."""
    parts, cur, last_break = [], [], None
    for start, text in snippets:
        t = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
        if not t or t in ("[Musica]", "[Music]", "[Applausi]", "[Applause]"):
            continue
        if last_break is None:
            last_break = start
        if start - last_break >= paragraph_every and cur:
            parts.append(" ".join(cur))
            cur, last_break = [], start
        cur.append(t)
    if cur:
        parts.append(" ".join(cur))
    return "\n\n".join(parts).strip()


# ─── Il freno ───────────────────────────────────────────────────────────────
# Quando YouTube risponde «troppe richieste» (429 / IpBlocked) l'errore non è di
# un video: è dell'indirizzo di casa, e insistere allunga il castigo. Al primo
# blocco l'agente si ferma per mezz'ora, poi un'ora, poi due, fino a sei; la
# prima trascrizione riuscita azzera tutto. Durante la pausa non si tocca
# YouTube e NON si prendono lavori dalla coda: restano lì, li si farà dopo.
BRAKE_STEPS = (30 * 60, 60 * 60, 2 * 3600, 4 * 3600, 6 * 3600)

# Errori che parlano dell'IP, non del video.
_BLOCK_ERRORS = {"IpBlocked", "RequestBlocked", "TooManyRequests", "YouTubeRequestFailed",
                 "IpBlockedError", "RequestBlockedError"}


def _is_block_error(exc) -> bool:
    if _err_names(exc) in _BLOCK_ERRORS:
        return True
    t = (str(exc) or "").lower()
    return "429" in t or "too many requests" in t or "blocking requests" in t


def brake_status() -> dict:
    """{'active','until','level','reason','left'} — 'left' sono i secondi che mancano."""
    try:
        st = (cfg.load_state().get("transcripts") or {})
    except Exception:
        st = {}
    until = float(st.get("brake_until", 0) or 0)
    left = max(0.0, until - time.time())
    return {"active": left > 0, "until": until, "left": left,
            "level": int(st.get("brake_level", 0) or 0), "reason": str(st.get("brake_reason", ""))}


def brake_label() -> str:
    """Frase pronta da mostrare (menu, pannello, log). Vuota se il freno non è tirato."""
    b = brake_status()
    if not b["active"]:
        return ""
    return "YouTube ci ha messo in pausa (troppe richieste): riprovo alle " + \
           time.strftime("%H:%M", time.localtime(b["until"]))


def _brake_pull(detail: str) -> float:
    """Tira il freno (o lo stringe, se era già tirato). Ritorna i secondi di pausa."""
    state = cfg.load_state()
    st = state.setdefault("transcripts", {})
    level = int(st.get("brake_level", 0) or 0)
    if float(st.get("brake_until", 0) or 0) <= time.time():
        pass                                   # pausa scaduta: si riparte dal gradino successivo
    wait = BRAKE_STEPS[min(level, len(BRAKE_STEPS) - 1)]
    st["brake_level"] = min(level + 1, len(BRAKE_STEPS) - 1)
    st["brake_until"] = time.time() + wait
    st["brake_reason"] = (detail or "troppe richieste")[:200]
    try:
        cfg.save_state(state)
    except Exception as e:
        log.debug("freno non salvato: %s", e)
    log.warning("Freno YouTube: pausa di %d minuti (%s)", wait // 60, st["brake_reason"])
    return wait


def brake_release(reason: str = "") -> None:
    """Toglie il freno: lo fa la prima trascrizione riuscita, o tu dal menu."""
    state = cfg.load_state()
    st = state.setdefault("transcripts", {})
    if not st.get("brake_until") and not st.get("brake_level"):
        return
    st["brake_until"] = 0
    st["brake_level"] = 0
    st["brake_reason"] = ""
    try:
        cfg.save_state(state)
    except Exception:
        pass
    log.info("Freno YouTube tolto%s", (" (" + reason + ")") if reason else "")


def get_transcript(video_id: str, languages=PREFERRED_LANGS) -> dict:
    """
    Ritorna {"status": "done"|"none"|"error", "text": str, "lang": str, "detail": str}.
      done  → sottotitoli scaricati (text pieno)
      none  → il video non ha sottotitoli (o non è accessibile): tocca a Gemini
      error → problema momentaneo (blocco IP, rete): da ritentare
    Compatibile con youtube-transcript-api 0.6 (statica) e 1.x (istanza).
    """
    if not YT_AVAILABLE:
        return {"status": "error", "text": "", "lang": "", "detail": "libreria youtube-transcript-api assente"}
    b = brake_status()
    if b["active"]:
        # in pausa: non si chiede niente a YouTube. L'elemento resta da ritentare.
        return {"status": "error", "text": "", "lang": "", "detail": brake_label()}
    try:
        if hasattr(YouTubeTranscriptApi, "list") and not isinstance(getattr(YouTubeTranscriptApi, "list"), staticmethod):
            api = YouTubeTranscriptApi()
            tl = api.list(video_id)                      # 1.x
        else:
            tl = YouTubeTranscriptApi.list_transcripts(video_id)   # 0.6
        transcript = None
        try:
            transcript = tl.find_transcript(list(languages))
        except Exception:
            # nessuna delle lingue preferite: prima quella manuale, poi l'automatica, poi qualsiasi
            avail = list(tl)
            manual = [t for t in avail if not getattr(t, "is_generated", False)]
            transcript = (manual or avail or [None])[0]
        if transcript is None:
            return {"status": "none", "text": "", "lang": "", "detail": "nessun sottotitolo per questo video"}
        fetched = transcript.fetch()
        text = _join(_snippets(fetched))
        lang = str(getattr(transcript, "language_code", "") or "")
        if getattr(transcript, "is_generated", False):
            lang = (lang + " auto").strip()
        if not text:
            return {"status": "none", "text": "", "lang": lang, "detail": "sottotitoli vuoti"}
        brake_release("trascrizione riuscita")   # ha funzionato: il freno non serve più
        return {"status": "done", "text": text, "lang": lang, "detail": ""}
    except Exception as e:
        if _is_none_error(e):
            return {"status": "none", "text": "", "lang": "", "detail": _err_names(e)}
        why = ("%s: %s" % (_err_names(e), str(e).splitlines()[0] if str(e).strip() else "")).strip(": ")
        if _is_block_error(e):
            _brake_pull(why)
            return {"status": "error", "text": "", "lang": "", "detail": brake_label() or why[:200]}
        log.debug("Trascrizione %s fallita: %s", video_id, why)
        return {"status": "error", "text": "", "lang": "", "detail": why[:200]}


# ─── Coda del sito ──────────────────────────────────────────────────────────
def _headers(conf: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if conf.get("token"):
        h["Authorization"] = f"Bearer {conf['token']}"
    return h


def _path(conf: dict) -> str:
    tr = conf.get("transcripts", {}) or {}
    return conf["server_url"].rstrip("/") + tr.get("poll_path", "/api/agent/transcribe")


def wake_watched(conf: dict, on_progress=None) -> dict:
    """
    Sveglia per le playlist e i canali sorvegliati dal sito: il lavoro lo fa il
    server (legge la playlist, inserisce i video nuovi), qui si bussa e basta.
    Ritorna {"checked": n, "new": n}. Silenziosa se l'endpoint non c'è (sito
    non ancora aggiornato) o se non c'è niente da guardare.
    """
    out = {"checked": 0, "new": 0}
    if not conf.get("token") or not conf.get("server_url"):
        return out
    url = conf["server_url"].rstrip("/") + "/api/agent/watch"
    try:
        r = httpx.get(url, params={"max": 1}, headers=_headers(conf), timeout=120)
        if r.status_code == 404:
            return out
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        log.debug("sveglia playlist sorvegliate fallita: %s", e)
        return out
    for c in (data.get("checked") or []):
        out["checked"] += 1
        out["new"] += int(c.get("new", 0) or 0)
        if on_progress and c.get("new"):
            on_progress(f"Playlist «{c.get('title') or c.get('list')}»: {c['new']} video nuovi")
    return out


def poll_and_run_once(conf: dict, on_progress=None) -> dict:
    """
    Un giro: preleva i video in coda, scarica i sottotitoli, rimanda i risultati,
    poi sveglia il fallback Gemini se c'è qualcosa in attesa.
    Ritorna {"jobs": n, "done": n, "none": n, "error": n, "fallback": n}.
    """
    res = {"jobs": 0, "done": 0, "none": 0, "error": 0, "fallback": 0, "braked": False}
    if not conf.get("token"):
        return res
    tr = conf.get("transcripts", {}) or {}
    if not tr.get("enabled", True):
        return res

    def prog(m):
        if on_progress:
            on_progress(m)

    # Freno tirato: non si prende nessun lavoro dalla coda (resterebbe «preso» senza
    # essere fatto) e non si tocca YouTube. Il fallback Gemini invece gira lo stesso:
    # lo esegue il server, col suo indirizzo, e non c'entra col nostro castigo.
    b = brake_status()
    if b["active"]:
        res["braked"] = True
        prog(brake_label())
        res["fallback"] = run_fallback(conf, on_progress=None, max_rounds=int(tr.get("fallback_rounds", 5) or 5))
        if res["fallback"]:
            prog(f"Gemini: {res['fallback']} trascritti · {brake_label()}")
        return res

    url = _path(conf)
    try:
        r = httpx.get(url, params={"limit": int(tr.get("batch", 5) or 5)}, headers=_headers(conf), timeout=30)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        log.debug("poll trascrizioni fallito: %s", e)
        return res
    if not data.get("ok"):
        log.debug("coda trascrizioni: %s", data.get("error"))
        return res

    jobs = data.get("jobs") or []
    results = []
    for i, job in enumerate(jobs):
        vid = job.get("video_id") or extract_video_id(job.get("url", ""))
        if not vid:
            results.append({"id": job.get("id"), "status": "none", "detail": "URL non riconosciuto"})
            continue
        prog(f"Trascrizioni: video {i + 1}/{len(jobs)} ({vid})")
        t = get_transcript(vid)
        results.append({"id": job.get("id"), "status": t["status"], "transcript": t["text"],
                        "lang": t["lang"], "detail": t["detail"]})
        res[t["status"]] = res.get(t["status"], 0) + 1
    res["jobs"] = len(jobs)

    fallback_pending = int(data.get("fallback_pending", 0) or 0)
    if results:
        try:
            r = httpx.post(url, json={"results": results}, headers=_headers(conf), timeout=60)
            r.raise_for_status()
            fallback_pending = int((r.json() or {}).get("fallback_pending", fallback_pending) or 0)
            log.info("Trascrizioni: %d video, %d con sottotitoli, %d senza, %d errori",
                     len(results), res["done"], res["none"], res["error"])
        except Exception as e:
            log.error("Invio trascrizioni fallito: %s", e)
    if results:
        telemetry.record_transcripts(res)

    if fallback_pending > 0 and data.get("gemini", True):
        res["fallback"] = run_fallback(conf, on_progress=prog, max_rounds=int(tr.get("fallback_rounds", 5) or 5))
    return res


def run_fallback(conf: dict, on_progress=None, max_rounds: int = 5) -> int:
    """
    Chiede al server di trascrivere con Gemini UN video senza sottotitoli alla
    volta, finché la coda è vuota o si esauriscono i giri. Ritorna quanti ha fatti.
    Ogni richiesta può durare minuti (Gemini ascolta il video): timeout lungo.
    """
    if not conf.get("token"):
        return 0
    url = _path(conf)
    done = 0
    for i in range(max(1, max_rounds)):
        if on_progress:
            on_progress(f"Gemini: trascrizione dei video senza sottotitoli ({i + 1})…")
        try:
            r = httpx.post(url, json={"fallback": 1}, headers=_headers(conf), timeout=330)
            r.raise_for_status()
            data = r.json() or {}
        except Exception as e:
            log.warning("fallback Gemini: richiesta fallita: %s", e)
            break
        proc = data.get("processed")
        if proc:
            done += 1
            log.info("Gemini: video %s → %s (%s caratteri)%s", proc.get("id"), proc.get("status"),
                     proc.get("chars", 0), (" · " + str(proc.get("error"))) if proc.get("error") else "")
            telemetry.record_fallback(proc)
        if not proc or int(data.get("remaining", 0) or 0) <= 0:
            break
    return done
