"""
Fetcher standalone — RSS/Atom, Substack, Reddit, YouTube (con trascrizioni).
Adattato da HAL backend/services/fetcher.py, senza dipendenze dal backend.
Gira sull'IP residenziale del PC dell'utente: trascrizioni gratuite, niente blocchi datacenter.
"""
import re
import logging
from datetime import datetime, timedelta

import feedparser
import httpx

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
    YT_AVAILABLE = True
except Exception:
    YT_AVAILABLE = False

log = logging.getLogger("hal_agent.fetcher")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


# ─── RSS / Atom (copre anche Substack e i canali YouTube) ───────────────────
def fetch_rss(feed_url: str, keywords: list, limit: int = 20) -> list:
    items = []
    try:
        feed = feedparser.parse(feed_url, request_headers=HEADERS)
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
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?limit={limit}"
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


def fetch_youtube_channel(channel_input: str, keywords: list, days_limit: int = 0, limit: int = 10) -> list:
    try:
        cid = _resolve_youtube_channel(channel_input)
    except ValueError as e:
        log.warning("%s", e)
        return []
    raw = fetch_rss(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", keywords, limit)
    if days_limit > 0:
        cutoff = datetime.now() - timedelta(days=days_limit)
        raw = [i for i in raw if _is_recent(i, cutoff)]
    for it in raw:
        vid = _extract_yt_id(it["url"])
        if vid:
            t = _get_transcript(vid)
            if t:
                it["excerpt"] = _clean(t, 800)
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
    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def _get_transcript(video_id: str):
    if not YT_AVAILABLE:
        return None
    try:
        parts = YouTubeTranscriptApi.get_transcript(video_id, languages=["it", "en", "en-US"])
        return " ".join(p["text"] for p in parts)
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except Exception as e:
        log.debug("Transcript error %s: %s", video_id, e)
        return None


def _clean(text: str, max_len: int = 500) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


# ─── Esecuzione di un agente ────────────────────────────────────────────────
def run_agent(agent: dict, days_limit: int = 0, progress=None) -> list:
    """Esegue il fetch per un agente (dict con keywords/rss_feeds/reddit_subreddits/youtube_channels)."""
    keywords = agent.get("keywords", [])
    results = []
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
            items = fetch_youtube_channel(target, keywords, days_limit=days_limit)
        for it in items:
            it["agent"] = agent.get("name", "")
        results.extend(items)
    return results
