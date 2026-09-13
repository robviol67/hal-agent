"""Playlist YouTube: da un indirizzo `…/playlist?list=…` agli ID dei video.

Serve alla cartella dei video (videos.py): nell'elenco si può incollare
l'indirizzo di una playlist invece dei singoli link, e l'agente la apre.
Niente chiave API e niente quota: si legge la pagina pubblica della playlist
(gli ID stanno nel JSON incorporato) e si continua con lo stesso meccanismo che
usa il browser per «caricare altri video» — 100 per pagina.

Una playlist resta SOTTO OSSERVAZIONE: viene riletta a ogni giro (con una
cache, per non bussare a YouTube ogni cinque minuti), quindi i video aggiunti
dopo arrivano da soli. I video tolti dalla playlist restano nel Feed: quello che
è già arrivato non si cancella da sé.
"""
import logging
import re

import httpx

log = logging.getLogger("hal_agent.playlists")

# id di playlist: PL… (normali), UU… (caricamenti di un canale), OL/LL/FL/RD…
_PLAYLIST_ID = re.compile(r"^(?:PL|UU|OL|LL|FL|RD|TL)[A-Za-z0-9_-]{8,}$")
_LIST_PARAM = re.compile(r"[?&]list=([A-Za-z0-9_-]{10,})")
_VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
_TOKEN = re.compile(r'"continuationCommand":\{"token":"([^"]+)"')
_KEY = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
_VER = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"')
_TITLE = re.compile(r'<meta name="title" content="([^"]*)"')

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "it,en;q=0.8",
    # In Europa YouTube sbatte davanti il muro del consenso e la pagina arriva senza
    # video: questo cookie è la risposta «ho già scelto» che manda il browser.
    "Cookie": "SOCS=CAI; CONSENT=YES+cb",
}


def extract_playlist_id(s: str):
    """ID della playlist da un indirizzo (o da un ID nudo). None se non è una playlist."""
    s = (s or "").strip()
    if _PLAYLIST_ID.match(s):
        return s
    m = _LIST_PARAM.search(s)
    return m.group(1) if m else None


def _unescape(s: str) -> str:
    return (s or "").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")


def playlist_videos(list_id: str, limit: int = 200, timeout: float = 25.0) -> dict:
    """
    ID dei video della playlist, in ordine. {'ok','ids','title','error','partial'}
    `limit` è un tetto di sicurezza: una playlist enorme non deve riempire il Feed
    in un colpo solo (e comunque l'invio è già limitato a max_per_run per giro).
    """
    out = {"ok": False, "ids": [], "title": "", "error": "", "partial": False}
    if not list_id:
        out["error"] = "playlist senza id"
        return out
    try:
        r = httpx.get("https://www.youtube.com/playlist",
                      params={"list": list_id, "hl": "it"},
                      headers=_HEADERS, timeout=timeout, follow_redirects=True)
    except Exception as e:
        out["error"] = f"non raggiungibile: {e}"
        return out
    if r.status_code != 200:
        out["error"] = f"HTTP {r.status_code}"
        return out
    if "consent." in str(r.url):
        out["error"] = "YouTube ha risposto con la pagina del consenso"
        return out

    html = r.text
    m = _TITLE.search(html)
    out["title"] = _unescape(m.group(1)) if m else ""
    ids = list(dict.fromkeys(_VIDEO_ID.findall(html)))
    if not ids:
        # playlist privata, vuota o eliminata: la pagina risponde ma non ha video
        out["error"] = "nessun video (playlist privata, vuota o rimossa?)"
        out["ok"] = False
        return out

    # «carica altri video»: stessa chiamata del browser, la chiave sta nella pagina
    key = _KEY.search(html)
    ver = _VER.search(html)
    tok = _TOKEN.search(html)
    token = tok.group(1) if tok else None
    if key and token:
        headers = dict(_HEADERS)
        headers["X-YouTube-Client-Name"] = "1"
        if ver:
            headers["X-YouTube-Client-Version"] = ver.group(1)
        client = {"clientName": "WEB", "clientVersion": ver.group(1) if ver else "2.20240101.00.00", "hl": "it"}
        pages = 0
        while token and len(ids) < limit and pages < 30:
            pages += 1
            try:
                rr = httpx.post("https://www.youtube.com/youtubei/v1/browse",
                                params={"key": key.group(1), "prettyPrint": "false"},
                                json={"context": {"client": client}, "continuation": token},
                                headers=headers, timeout=timeout)
                if rr.status_code != 200:
                    break
                body = rr.text
            except Exception as e:
                log.debug("playlist %s: pagina %d non letta: %s", list_id, pages, e)
                break
            before = len(ids)
            for v in _VIDEO_ID.findall(body):
                if v not in ids:
                    ids.append(v)
            m = _TOKEN.search(body)
            token = m.group(1) if m else None
            if len(ids) == before:       # niente di nuovo: meglio fermarsi
                break

    if len(ids) > limit:
        ids = ids[:limit]
        out["partial"] = True
    elif token:
        out["partial"] = True
    out["ids"] = ids
    out["ok"] = True
    return out
