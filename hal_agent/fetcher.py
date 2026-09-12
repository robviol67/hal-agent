"""
Fetcher standalone — RSS/Atom, Substack, Reddit, YouTube (con trascrizioni intere, vedi transcripts.py).
Adattato da HAL backend/services/fetcher.py, senza dipendenze dal backend.
Gira sull'IP residenziale del PC dell'utente: trascrizioni gratuite, niente blocchi datacenter.
"""
import re
import logging
from datetime import datetime, timedelta

import feedparser
import httpx

from . import transcripts

log = logging.getLogger("hal_agent.fetcher")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Fonti che hanno fallito durante l'ultimo run_agent: [(indirizzo, motivo), …].
# Prima finivano solo nel log e lo Scout sembrava semplicemente "vuoto"; così
# invece il Pannello può dire QUALE fonte non risponde e perché.
_ERRORS = []


def _note_error(target: str, msg) -> None:
    # solo la prima riga: httpx allega un pistolotto multiriga con link ai docs
    _ERRORS.append((str(target), str(msg).splitlines()[0][:160] if str(msg).strip() else "errore"))


# ─── RSS / Atom (copre anche Substack e i canali YouTube) ───────────────────
def fetch_rss(feed_url: str, keywords: list, limit: int = 20) -> list:
    items = []
    try:
        # Scarichiamo NOI il feed con httpx invece di lasciar fare a feedparser.
        # feedparser userebbe urllib, che nell'app impacchettata (PyInstaller) non
        # trova i certificati CA: ogni feed falliva la verifica SSL e tornava vuoto
        # SENZA sollevare eccezioni (bozo) — raccolta a zero, in silenzio.
        # httpx porta con sé i propri certificati e solleva errori veri.
        resp = httpx.get(feed_url, headers=HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.get("bozo") and not feed.entries:
            log.warning("Feed illeggibile %s: %s", feed_url, feed.get("bozo_exception"))
            _note_error(feed_url, "feed illeggibile: %s" % feed.get("bozo_exception"))
            return items
        if not feed.entries:
            log.info("Feed senza elementi: %s", feed_url)
        kw = [k.lower() for k in keywords]
        for entry in feed.entries[:limit]:
            title = (entry.get("title") or "").strip()
            summary = entry.get("summary", "") or entry.get("description", "")
            url = entry.get("link", "")
            if kw:
                text = (title + " " + summary).lower()
                if not any(k in text for k in kw):
                    continue
            published = ""
            for key in ("published", "updated", "created"):
                if entry.get(key):
                    published = entry.get(key); break
            channel = feed.feed.get("title", "") if hasattr(feed, "feed") else ""
            author = entry.get("author", "") or (entry.get("author_detail", {}) or {}).get("name", "")
            items.append({
                "title": title,
                "excerpt": _clean(summary, 500),
                "url": url,
                "source": _detect_source(feed_url),
                "published": published,
                "channel": channel,
                "author": author,
            })
    except Exception as e:
        log.warning("RSS error %s: %s", feed_url, e)
        _note_error(feed_url, e)
    return items


def _detect_source(url: str) -> str:
    if "youtube.com" in url:
        return "youtube"
    if "substack.com" in url:
        return "substack"
    if "reddit.com" in url:
        return "reddit"
    return "rss"


# ─── Reddit (via RSS pubblico) ──────────────────────────────────────────────
def fetch_reddit(subreddit: str, keywords: list, limit: int = 20) -> list:
    # Accetta "python", "r/python" e "/r/python": scritto com'è, "r/python"
    # diventava ".../r/r/python/..." → 404 silenzioso.
    name = str(subreddit).strip().strip("/")
    if name.lower().startswith("r/"):
        name = name[2:]
    url = f"https://www.reddit.com/r/{name}/new.rss?limit={limit}"
    items = fetch_rss(url, keywords, limit)
    for it in items:
        it["source"] = "reddit"
    return items


# ─── YouTube canale (RSS) + trascrizione ────────────────────────────────────
def _resolve_youtube_channel(channel_input: str) -> str:
    s = channel_input.strip()
    if re.match(r"^UC[a-zA-Z0-9_-]{22}$", s):
        return s
    m = re.search(r"youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})", s)
    if m:
        return m.group(1)
    handle = s.lstrip("@").split("/")[-1].lstrip("@")
    mobile = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
              "Accept-Language": "en-US,en;q=0.9"}
    try:
        r = httpx.get(f"https://www.youtube.com/@{handle}", headers=mobile, timeout=10, follow_redirects=True)
        for pat in (r'"browseId":"(UC[a-zA-Z0-9_-]{22})"',
                    r'"channelId":"(UC[a-zA-Z0-9_-]{22})"',
                    r'youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})'):
            m = re.search(pat, r.text)
            if m:
                return m.group(1)
    except Exception as e:
        log.warning("Handle @%s non risolto: %s", handle, e)
    raise ValueError(f"Canale YouTube non risolvibile: {channel_input}")


def fetch_youtube_channel(channel_input: str, keywords: list, days_limit: int = 0, limit: int = 10,
                          transcribe: bool = True, progress=None) -> list:
    """
    Video recenti di un canale (RSS) e, se `transcribe`, la trascrizione INTERA di
    ciascuno (sottotitoli YouTube): viaggia nel campo `transcript` con
    `transcript_status` = done | none | error, e il server la salva accanto al
    video nel Feed. L'estratto resta un'anteprima. Se i sottotitoli non ci sono
    (none) sarà il server a passare il video a Gemini.
    """
    try:
        cid = _resolve_youtube_channel(channel_input)
    except ValueError as e:
        log.warning("%s", e)
        _note_error(channel_input, e)
        return []
    raw = fetch_rss(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", keywords, limit)
    if days_limit > 0:
        cutoff = datetime.now() - timedelta(days=days_limit)
        raw = [i for i in raw if _is_recent(i, cutoff)]
    if not transcribe:
        return raw
    for n, it in enumerate(raw):
        vid = _extract_yt_id(it["url"])
        if not vid:
            continue
        if progress:
            progress(f"trascrizione {n + 1}/{len(raw)}")
        t = transcripts.get_transcript(vid)
        it["transcript"] = t["text"]
        it["transcript_status"] = t["status"]
        it["transcript_detail"] = t["detail"]
        if t["text"]:
            it["excerpt"] = _clean(t["text"], 800)
    return raw


def _is_recent(item: dict, cutoff) -> bool:
    pub = item.get("published") or ""
    if not pub:
        return True
    try:
        return datetime.fromisoformat(pub.replace("Z", "")) >= cutoff
    except Exception:
        return True


def _extract_yt_id(url: str):
    return transcripts.extract_video_id(url)


def _clean(text: str, max_len: int = 500) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


# ─── Esecuzione di un agente ────────────────────────────────────────────────
def run_agent(agent: dict, days_limit: int = 0, progress=None, errors=None) -> list:
    """
    Esegue il fetch per un agente (dict con keywords/rss_feeds/reddit_subreddits/youtube_channels).
    Se `errors` è una lista, ci finiscono le fonti che hanno fallito: [(indirizzo, motivo), …].
    """
    keywords = agent.get("keywords", [])
    results = []
    del _ERRORS[:]
    tasks = ([("rss", u) for u in agent.get("rss_feeds", [])]
             + [("reddit", s) for s in agent.get("reddit_subreddits", [])]
             + [("youtube", c) for c in agent.get("youtube_channels", [])])
    for idx, (kind, target) in enumerate(tasks):
        if progress:
            progress(idx, len(tasks), kind, target)
        if kind == "rss":
            items = fetch_rss(target, keywords)
            if days_limit > 0:
                cutoff = datetime.now() - timedelta(days=days_limit)
                items = [i for i in items if _is_recent(i, cutoff)]
        elif kind == "reddit":
            items = fetch_reddit(target, keywords)
        else:
            # yt_transcribe arriva dal sito (per Scout); se manca, si trascrive
            def yt_prog(msg, _i=idx, _n=len(tasks), _t=target):
                if progress:
                    progress(_i, _n, "youtube", f"{_t} · {msg}")
            # Un canale YouTube è già tematico: si prende INTERO, senza il filtro per
            # parole chiave (che agirebbe su titolo e descrizione, spesso fuorvianti).
            # Le parole chiave filtrano solo RSS e Reddit.
            items = fetch_youtube_channel(target, [], days_limit=days_limit,
                                          transcribe=bool(agent.get("yt_transcribe", True)),
                                          progress=yt_prog)
        for it in items:
            it["agent"] = agent.get("name", "")
        results.extend(items)
    if errors is not None:
        errors.extend(_ERRORS)
    return results
