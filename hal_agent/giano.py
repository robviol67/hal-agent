"""Capability «Giano» (dalla v0.8.0): la cartella ~/Giano del Mac e Giano su hal-ai.it.

Regola di fondo: per NOME e POSIZIONE fa fede il web. Se in Giano un file viene
rinominato o spostato (anche confermando una proposta di «Da smistare»), l'agente
lo rinomina e lo sposta sul Mac. Per il CONTENUTO decide l'utente.

Registro locale: ~/.hal-agent/giano_registro.json
    {"files": {"<file_id>": {"path": "Clienti/X/a.pdf", "version": 3, "sha": "…",
                              "size": 123, "mtime": 1.0, "pending_sha": "…", "ignored_sha": "…"}},
     "skipped": {"<percorso>": <dimensione>}}

Un giro:
  1. hello + sync: che cosa ha Giano, che cosa chiede, quali decisioni sono state prese;
  2. esegue le decisioni (aggiorna · duplica · ignora);
  3. allinea al web: sposta/rinomina, scarica le versioni nuove e i file chiesti
     («Porta sul Mac», «Riscarica», «Aggiorna il Mac»), mette da parte i file
     eliminati in Giano (cartella nascosta .giano-cestino, mai cancellati davvero);
  4. legge la cartella: i file nuovi salgono (con le sottocartelle), i cancellati si
     segnalano («solo online»), i modificati salgono «in sospeso» e chiedono una decisione.
"""
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

import httpx

from . import config as cfg

log = logging.getLogger("hal_agent.giano")

REGISTRY = cfg.CONFIG_DIR / "giano_registro.json"
# v0.9.3 — il pannello di Giano: il giro scrive, il pannello (altro processo) legge
STATE = cfg.CONFIG_DIR / "giano_stato.json"        # situazione: ultimo giro, in corso, spazio, errori
WEBLIST = cfg.CONFIG_DIR / "giano_web.json"        # tutti i file di Giano (azione «overview»)
ACTIVITY = cfg.CONFIG_DIR / "giano_attivita.jsonl" # un evento per riga
CMD_DIR = cfg.CONFIG_DIR / "giano_comandi"         # i comandi del pannello, un file ciascuno
LOCK = cfg.CONFIG_DIR / "giano.lock"               # un giro alla volta
OVERVIEW_EVERY = 600                               # secondi fra due «overview» se non cambia niente
TRASH_DIR = ".giano-cestino"
_SKIP_PREFIX = (".", "~$")
_SKIP_SUFFIX = (".crdownload", ".part", ".tmp", ".download", ".giano-tmp")


def _version() -> str:
    from . import __version__
    return __version__


# ─────────────────────────────── registro ───────────────────────────────
def load_registry() -> dict:
    try:
        d = json.loads(REGISTRY.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("files", {})
            d.setdefault("skipped", {})
            return d
    except Exception:
        pass
    return {"files": {}, "skipped": {}}


def save_registry(reg: dict) -> None:
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, REGISTRY)


# ─────────────────────────── stato, attività, comandi ───────────────────────────
def read_json(path, default):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


def write_json(path, data) -> None:
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def state_update(**kw) -> dict:
    """Aggiorna giano_stato.json (lo scrive solo il giro)."""
    s = read_json(STATE, {})
    s.update(kw)
    try:
        write_json(STATE, s)
    except Exception as e:
        log.debug("stato non scritto: %s", e)
    return s


def activity(kind: str, text: str, ok: bool = True, fid=None) -> None:
    """Un evento nello storico del pannello. kind: invio | scarico | sposto | cestino | modifica | decisione | ocr | errore | comando"""
    try:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": time.time(), "k": kind, "x": text, "ok": ok, "fid": fid}, ensure_ascii=False)
        with open(ACTIVITY, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if ACTIVITY.stat().st_size > 2_000_000:          # tiene la metà più recente
            lines = ACTIVITY.read_text(encoding="utf-8").splitlines()
            ACTIVITY.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
    except Exception as e:
        log.debug("attività non scritta: %s", e)


def read_activity(n: int = 500) -> list:
    try:
        lines = ACTIVITY.read_text(encoding="utf-8").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def send_command(name: str, arg="") -> None:
    """Dal pannello: allinea | elenco | porta:<id> | riscarica:<id> | svuota_cestino"""
    CMD_DIR.mkdir(parents=True, exist_ok=True)
    (CMD_DIR / f"{time.time_ns()}_{name}").write_text(str(arg), encoding="utf-8")


def pending_commands() -> bool:
    try:
        return any(CMD_DIR.iterdir())
    except Exception:
        return False


def take_commands() -> list:
    out = []
    try:
        for f in sorted(CMD_DIR.iterdir()):
            try:
                out.append((f.name.split("_", 1)[1], f.read_text(encoding="utf-8").strip()))
            except Exception:
                pass
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass
    return out


def _dir_size(path: str, skip_trash: bool = False) -> tuple:
    total, n = 0, 0
    for d, dirs, files in os.walk(path):
        if skip_trash:
            dirs[:] = [x for x in dirs if x != TRASH_DIR]
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
                n += 1
            except OSError:
                pass
    return total, n


def local_space(root: str) -> dict:
    size, n = _dir_size(root, skip_trash=True) if os.path.isdir(root) else (0, 0)
    tsize, tn = _dir_size(os.path.join(root, TRASH_DIR)) if os.path.isdir(os.path.join(root, TRASH_DIR)) else (0, 0)
    try:
        du = shutil.disk_usage(root if os.path.isdir(root) else str(Path.home()))
        free, disk = du.free, du.total
    except Exception:
        free, disk = 0, 0
    return {"folder_bytes": size, "folder_files": n, "trash_bytes": tsize, "trash_files": tn, "disk_free": free, "disk_total": disk}


def empty_local_trash(root: str) -> int:
    """Svuota ~/Giano/.giano-cestino (chiesto dal pannello, con conferma). Ritorna i byte liberati."""
    tdir = os.path.join(root, TRASH_DIR)
    if not os.path.isdir(tdir):
        return 0
    size, _ = _dir_size(tdir)
    shutil.rmtree(tdir, ignore_errors=True)
    return size


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _web_rel(e: dict) -> str:
    return (e["dir"] + "/" if e.get("dir") else "") + e["name"]


# ──────────────────────────────── server ────────────────────────────────
class Server:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Bearer {token}"}
        self.c = httpx.Client(timeout=120, headers=self.h)
        self.chunk = 8 * 1024 * 1024
        self.max_file = 200_000_000

    def agent(self, action: str, body: dict = None):
        url = f"{self.base}/api/giano/agent.php?action={action}"
        r = self.c.post(url, json=body or {}) if body is not None else self.c.get(url)
        r.raise_for_status()
        return r.json()

    def upload(self, path: str, name: str, extra_finish: dict = None, on_progress=None) -> dict:
        """Carica un file a pezzi. Ritorna la risposta di finish (o di start, se è un doppione)."""
        size = os.path.getsize(path)
        u = f"{self.base}/api/giano/upload.php"
        r = self.c.post(u + "?action=start", json={"name": name, "size": size, "origin": "agente"})
        if r.status_code == 413:
            return {"status": "rifiutato", "error": (r.json() or {}).get("error")}
        r.raise_for_status()
        s = r.json()
        uid, off = s["upload_id"], int(s.get("received", 0))
        cs = int(s.get("chunk_size") or self.chunk)
        if s.get("status") == "doppione" and not (extra_finish or {}).get("force"):
            return s
        with open(path, "rb") as fh:
            while off < size:
                fh.seek(off)
                piece = fh.read(cs)
                base = off

                def stream(piece=piece, base=base):
                    # a tratti di 256 KB, per una percentuale che scorre (la lunghezza resta dichiarata)
                    step = 256 * 1024
                    for i in range(0, len(piece), step):
                        yield piece[i:i + step]
                        if on_progress:
                            on_progress(min(size, base + i + step), size)
                rr = self.c.post(u, params={"action": "chunk", "upload_id": uid, "offset": off}, content=stream(),
                                 headers={**self.h, "Content-Type": "application/octet-stream", "Content-Length": str(len(piece))})
                if rr.status_code == 409:
                    off = int((rr.json() or {}).get("received", off))
                    continue
                rr.raise_for_status()
                off = int(rr.json()["received"])
        f = self.c.post(u + "?action=finish", json={"upload_id": uid, **(extra_finish or {})})
        f.raise_for_status()
        out = f.json()
        out["upload_id"] = uid
        return out

    def finish(self, upload_id: str, extra: dict) -> dict:
        f = self.c.post(f"{self.base}/api/giano/upload.php?action=finish", json={"upload_id": upload_id, **extra})
        f.raise_for_status()
        return f.json()

    def download(self, file_id: int, dest: str, on_progress=None) -> str:
        """Scarica la versione corrente in dest (prima in un file temporaneo). Ritorna lo sha256."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".giano-tmp"
        h = hashlib.sha256()
        with self.c.stream("GET", f"{self.base}/api/giano/file.php", params={"id": file_id, "dl": 1}) as r:
            r.raise_for_status()
            total, got, last = int(r.headers.get("content-length") or 0), 0, -1
            with open(tmp, "wb") as fh:
                for b in r.iter_bytes(1 << 20):
                    fh.write(b)
                    h.update(b)
                    got += len(b)
                    if on_progress and total:
                        pct = int(got * 100 / total)
                        if pct != last:
                            last = pct
                            on_progress(pct)
        os.replace(tmp, dest)
        return h.hexdigest()


# ─────────────────────────────── un giro ────────────────────────────────
def _free_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def _to_trash(root: str, path: str) -> None:
    if not os.path.exists(path):
        return
    dest = _free_path(os.path.join(root, TRASH_DIR, _rel(root, path)))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(path, dest)


def _iter_local(root: str):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for n in files:
            if n.startswith(_SKIP_PREFIX) or n.lower().endswith(_SKIP_SUFFIX):
                continue
            yield os.path.join(d, n)


def _pdf_text(path: str) -> str:
    try:
        from . import docmd
        return docmd._pdf_md(path)
    except Exception as e:
        log.debug("testo del PDF non letto: %s", e)
        return ""


def sync_once(conf: dict = None, on_progress=None) -> dict:
    """Un giro, con il lucchetto (mai due giri insieme) e lo stato per il pannello."""
    import fcntl
    conf = conf or cfg.load_config()
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lockf.close()
        return {"ok": False, "error_msg": "c'è già un giro in corso"}
    last = [0.0]

    def prog(m):
        if on_progress:
            on_progress(m)
        now = time.time()
        if now - last[0] > 0.7:                # il pannello lo legge: non a ogni byte
            last[0] = now
            state_update(running=True, step=m)

    started = time.time()
    state_update(running=True, step="Giano: comincio il giro", started=started)
    res, srv = {"ok": False}, None
    try:
        res, srv = _round(conf, prog)
        if res.pop("_check", False):
            check_all(conf, prog, res)
    except Exception as e:
        log.exception("giro di Giano fallito")
        res = {"ok": False, "error_msg": str(e)[:200]}
    finally:
        try:
            _after_round(conf, res, srv, started)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()
    return res


def _after_round(conf: dict, res: dict, srv, started: float) -> None:
    g = conf.get("giano", {}) or {}
    root = os.path.expanduser(g.get("folder") or str(Path.home() / "Giano"))
    st = read_json(STATE, {})
    changed = any(res.get(k) for k in ("sent", "downloaded", "moved", "deleted", "changed"))
    want = res.pop("_want_overview", False)
    over = None
    if srv is not None and res.get("ok") and (want or changed or time.time() - st.get("overview_at", 0) > OVERVIEW_EVERY):
        try:
            over = srv.agent("overview")
            write_json(WEBLIST, {"at": time.time(), "files": over.get("files", []), "decisions": over.get("decisions", [])})
        except Exception as e:
            log.info("panoramica di Giano non letta: %s", e)
    fails = 0 if res.get("ok") else int(st.get("fails", 0)) + 1
    kw = dict(running=False, step="", last_run=time.time(), last_ok=bool(res.get("ok")), fails=fails,
              last_result={k: res.get(k, 0) for k in ("sent", "downloaded", "moved", "deleted", "changed", "errors", "ocr")},
              error=res.get("error_msg") or "", duration=round(time.time() - started, 1),
              folder=root, local=local_space(root), server=conf.get("server_url") or "")
    if res.get("ok"):
        kw["last_good"] = time.time()
    if over:
        kw.update(online=over.get("storage") or {}, overview_at=time.time())
    if not res.get("ok") and res.get("error_msg"):
        activity("errore", "Giro non riuscito: " + res["error_msg"], ok=False)
    res["notify"] = _notifications(g, res, st, kw)
    state_update(**kw)


def _notifications(g: dict, res: dict, before: dict, kw: dict) -> list:
    """Le notifiche di macOS dopo un giro (v0.9.5). g['notify']: importanti | tutto | niente.
    Ogni avviso parte UNA volta: i segni restano in giano_stato.json."""
    mode = g.get("notify") or "importanti"
    out = []
    # file che aspettano una decisione: si avvisa quando aumentano
    try:
        w = read_json(WEBLIST, {})
        reg = load_registry().get("files", {})
        ids = {str(d.get("file_id")) for d in w.get("decisions", [])} | {k for k, r in reg.items() if r.get("pending_sha")}
        n_dec = len(ids)
    except Exception:
        n_dec = int(before.get("notified_dec", 0))
    if n_dec > int(before.get("notified_dec", 0)):
        out.append(f"{n_dec} file {'aspetta' if n_dec == 1 else 'aspettano'} una decisione: cambiati sul Mac dopo l'invio. "
                   "Scegli nel pannello di Giano.")
    kw["notified_dec"] = n_dec
    # tre giri falliti di fila
    if kw.get("fails") == 3:
        out.append("Giano non riesce ad allinearsi da tre giri: " + (res.get("error_msg") or "errore sconosciuto") + ". Riprovo da solo.")
    # disco quasi pieno: una volta al giorno
    free = (kw.get("local") or {}).get("disk_free") or 0
    today = time.strftime("%Y-%m-%d")
    if free and free < 5_000_000_000 and before.get("notified_disk") != today:
        out.append(f"Disco quasi pieno: restano {free / 1e9:.1f} GB. Giano può portare sul Mac meno file.".replace(".", ",", 1))
        kw["notified_disk"] = today
    # esito di «Controlla tutto»
    chk = res.get("check")
    if chk:
        out.append(f"Controllo finito: {chk['n']} file verificati, tutti uguali a Giano." if not chk["bad"]
                   else (f"Controllo finito: {len(chk['bad'])} file del Mac non sono uguali a quelli di Giano. Li trovi fra «Da decidere»." if len(chk["bad"]) > 1
                         else "Controllo finito: 1 file del Mac non è uguale a quello di Giano. Lo trovi fra «Da decidere»."))
    if mode == "tutto":
        r = res
        bits = [f"{r[k]} {lab}" for k, lab in (("sent", "inviati"), ("downloaded", "scaricati"), ("moved", "spostati")) if r.get(k)]
        if bits:
            out.append("Giano: " + ", ".join(bits) + ".")
    if mode == "niente":
        return []
    return out


def check_all(conf: dict, prog, res: dict) -> None:
    """«Controlla tutto» (v0.9.5): rilegge ogni file del Mac e lo confronta con l'impronta che
    Giano conosce. Un file diverso (cambiato senza che il giro se ne accorgesse, o rovinato)
    viene ripreso al prossimo giro come «cambiato sul Mac»: decidi tu."""
    g = conf.get("giano", {}) or {}
    root = os.path.expanduser(g.get("folder") or str(Path.home() / "Giano"))
    reg = load_registry()
    F = reg["files"]
    items = [(fid, r) for fid, r in F.items() if os.path.exists(os.path.join(root, r["path"]))]
    total = sum(int(r.get("size") or 0) for _, r in items) or 1
    done, bad = 0, []
    for i, (fid, r) in enumerate(items):
        path = os.path.join(root, r["path"])
        prog(f"Giano: controllo «{os.path.basename(path)}» — {int(done * 100 / total)}% ({i + 1} di {len(items)})")
        try:
            sha = _sha(path)
        except OSError as e:
            bad.append(r["path"])
            activity("errore", f"«{os.path.basename(path)}» non si legge: {e}", ok=False, fid=int(fid))
            continue
        done += int(r.get("size") or 0)
        if sha != r["sha"] and sha not in (r.get("pending_sha"), r.get("ignored_sha")):
            bad.append(r["path"])
            r["mtime"] = 0          # il prossimo giro lo rilegge e lo tratta come cambiato
            activity("modifica", f"«{os.path.basename(path)}» sul Mac non è uguale a quello di Giano: lo riprendo al prossimo giro", ok=False, fid=int(fid))
    save_registry(reg)
    res["check"] = {"n": len(items), "bad": bad}
    state_update(check={"at": time.time(), "n": len(items), "bad": len(bad)})
    activity("comando", f"Controllo finito: {len(items)} file verificati" + (", tutti uguali a Giano" if not bad else f", {len(bad)} diversi"))


def _commands(srv, root: str, res: dict, prog) -> None:
    """I comandi lasciati dal pannello."""
    for name, arg in take_commands():
        try:
            if name in ("porta", "riscarica"):
                ids = [int(x) for x in str(arg).split(",") if x.strip().isdigit()]
                for fid in ids:
                    srv.agent("request", {"file_id": fid, "what": name})
                if ids:
                    activity("comando", ("Chiesto di portare sul Mac " if name == "porta" else "Chiesto di riscaricare ")
                             + (f"il file {ids[0]}" if len(ids) == 1 else f"{len(ids)} file"), fid=ids[0] if len(ids) == 1 else None)
            elif name == "decidi":
                fid, choice = str(arg).split(":", 1)
                ans = srv.agent("decide", {"file_id": int(fid), "choice": choice})
                if not ans.get("ok"):
                    raise RuntimeError(ans.get("error") or "decisione rifiutata")
                activity("decisione", {"aggiorna": "Scelto «Aggiorna»", "duplica": "Scelto «Duplica»", "ignora": "Scelto «Ignora»"}.get(choice, choice)
                         + f" per il file {fid}: lo eseguo adesso", fid=int(fid))
            elif name == "svuota_cestino":
                freed = empty_local_trash(root)
                from .progress import fmt_size
                activity("cestino", f"Svuotato il cestino locale: liberati {fmt_size(freed)}")
                res["_want_overview"] = True
            elif name == "elenco":
                res["_want_overview"] = True
            elif name == "controlla":
                res["_check"] = True
        except Exception as e:
            activity("errore", f"Comando «{name}» non eseguito: {e}", ok=False)


def excluded(rel: str, size: int, g: dict) -> str:
    """Perché un file NUOVO della cartella non sale (v0.9.4): '' se sale. Vale solo per i file
    nuovi: quelli già in Giano restano allineati anche se poi li si esclude."""
    name = os.path.basename(rel).lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    exts = {e.strip().lower().lstrip(".") for e in (g.get("exclude_ext") or []) if e.strip()}
    if ext and ext in exts:
        return f"estensione .{ext} esclusa"
    parts = [x.lower() for x in rel.split("/")[:-1]]
    for orig in (g.get("exclude_dirs") or []):
        d = orig.strip().strip("/").lower()
        if d and ("/" in d and ("/".join(parts) + "/").startswith(d + "/") or d in parts):
            return f"cartella «{orig.strip().strip('/')}» esclusa"
    mb = float(g.get("max_mb") or 0)
    if mb > 0 and size > mb * 1_000_000:
        return f"più grande di {mb:g} MB"
    return ""


def _round(conf: dict, prog) -> tuple:
    g = conf.get("giano", {}) or {}
    res = {"ok": True, "sent": 0, "downloaded": 0, "moved": 0, "deleted": 0, "changed": 0, "errors": 0}

    token, base = conf.get("token") or "", conf.get("server_url") or ""
    if not token or not base:
        res.update(ok=False, error_msg="collegamento non configurato")
        return res, None
    root = os.path.expanduser(g.get("folder") or str(Path.home() / "Giano"))
    os.makedirs(root, exist_ok=True)
    srv = Server(base, token)
    reg = load_registry()
    F = reg["files"]

    try:
        h = srv.agent("hello", {"folder": root.replace(str(Path.home()), "~"), "version": _version()})
        srv.chunk = int(h.get("chunk") or srv.chunk)
        srv.max_file = int(h.get("max_file") or srv.max_file)
        _commands(srv, root, res, prog)
        data = srv.agent("sync")
    except Exception as e:
        res.update(ok=False, error_msg=str(e)[:200])
        return res, None
    web = {str(e["id"]): e for e in data.get("files", [])}
    open_pending = {str(o["file_id"]): o["pending_sha"] for o in data.get("open", [])}

    def path_of(fid):
        return os.path.join(root, F[fid]["path"]) if fid in F else None

    def register(fid, rel, version, sha, path):
        st = os.stat(path)
        F[str(fid)] = {"path": rel, "version": int(version), "sha": sha, "size": st.st_size, "mtime": st.st_mtime}

    # ── 2. decisioni prese sul web
    for d in data.get("decided", []):
        fid = str(d["file_id"])
        try:
            if fid in F:
                r = F[fid]
                if d["choice"] == "aggiorna":
                    r["sha"] = d["pending_sha"]
                    r.pop("pending_sha", None)
                    if fid in web:
                        r["version"] = web[fid]["version"]
                elif d["choice"] == "ignora":
                    r["ignored_sha"] = d["pending_sha"]
                    r.pop("pending_sha", None)
                    # conflitto: resta corrente la versione di Giano, e il Mac la riceve
                    # (quella del Mac è salvata in Giano come versione passata)
                    if fid in web and web[fid]["version"] > r["version"]:
                        cur = path_of(fid)
                        sha = srv.download(int(fid), cur)
                        register(fid, r["path"], web[fid]["version"], sha, cur)
                        srv.agent("synced", {"file_id": int(fid), "version": web[fid]["version"]})
                elif d["choice"] == "duplica" and d.get("result_file_id"):
                    # il file locale modificato diventa la copia «(dal Mac)»; l'originale si riscarica
                    nid = str(d["result_file_id"])
                    src = path_of(fid)
                    if src and os.path.exists(src) and nid in web:
                        dest = _free_path(os.path.join(root, _web_rel(web[nid])))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.move(src, dest)
                        register(nid, _rel(root, dest), 1, d["pending_sha"], dest)
                        srv.agent("synced", {"file_id": int(nid), "version": 1})
                    if fid in web:          # l'originale torna com'è in Giano, al suo posto
                        orig = os.path.join(root, _web_rel(web[fid]))
                        sha = srv.download(int(fid), orig)
                        register(fid, _rel(root, orig), web[fid]["version"], sha, orig)
                        srv.agent("synced", {"file_id": int(fid), "version": web[fid]["version"]})
                    else:
                        F.pop(fid, None)
            srv.agent("ack", {"id": d["id"]})
            nm = (web.get(fid) or {}).get("name") or (F.get(fid) or {}).get("path", fid)
            activity("decisione", {"aggiorna": "Aggiornato", "ignora": "Tenuta la versione di Giano per", "duplica": "Duplicato"}.get(d["choice"], d["choice"]) + f" «{os.path.basename(str(nm))}»", fid=int(fid))
        except Exception as e:
            log.warning("decisione %s non eseguita: %s", d.get("id"), e)
            activity("errore", f"Decisione sul file {fid} non eseguita: {e}", ok=False, fid=int(fid))
            res["errors"] += 1
    save_registry(reg)

    def open_on_mac(fid, path, name):
        # «Modifica sul Mac»: il file si apre nel suo programma; ciò che si salva diventa la versione nuova
        import subprocess
        prog(f"Giano: apro «{name}» sul Mac")
        subprocess.Popen(["open", path])
        srv.agent("edit_open", {"file_id": int(fid)})

    # ── 3. allinea al web
    for fid, e in web.items():
        try:
            target_rel = _web_rel(e)
            target = os.path.join(root, target_rel)
            if e.get("trashed"):
                if fid in F:
                    _to_trash(root, path_of(fid))
                    del F[fid]
                    res["deleted"] += 1
                    activity("cestino", f"«{e['name']}» eliminato in Giano: messo nel cestino locale", fid=int(fid))
                continue
            if e.get("request") == "word" and e.get("request_state") != "in_corso":
                if fid not in F or not os.path.exists(path_of(fid)):
                    dest = _free_path(target)
                    sha = srv.download(int(fid), dest, lambda p, n=e['name']: prog(f"Giano: scarico «{n}» — {p}%"))
                    register(fid, _rel(root, dest), e["version"], sha, dest)
                    srv.agent("synced", {"file_id": int(fid), "version": e["version"]})
                open_on_mac(fid, path_of(fid), e["name"])
                continue
            if fid in F:
                r = F[fid]
                cur = path_of(fid)
                if os.path.exists(cur) and r["path"] != target_rel:
                    if not os.path.exists(target):
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.move(cur, target)
                        activity("sposto", f"Spostato «{e['name']}» → {e.get('dir') or 'cartella principale'}", fid=int(fid))
                        r["path"] = target_rel
                        cur = target
                        res["moved"] += 1
                if not os.path.exists(cur):
                    if e.get("request") in ("riscarica", "porta", "aggiorna"):
                        prog(f"Giano: scarico «{e['name']}»")
                        sha = srv.download(int(fid), target, lambda p, n=e['name']: prog(f"Giano: scarico «{n}» — {p}%"))
                        register(fid, target_rel, e["version"], sha, target)
                        srv.agent("synced", {"file_id": int(fid), "version": e["version"]})
                        res["downloaded"] += 1
                        activity("scarico", f"Riscaricato «{e['name']}» (v{e['version']})", fid=int(fid))
                    continue
                local_same = False
                st = os.stat(cur)
                if st.st_size == r.get("size") and abs(st.st_mtime - r.get("mtime", 0)) < 1e-3:
                    local_same = True
                elif _sha(cur) == r["sha"]:
                    local_same = True
                if (e["version"] > r["version"] and local_same) or e.get("request") == "aggiorna":
                    prog(f"Giano: aggiorno «{e['name']}»")
                    sha = srv.download(int(fid), cur, lambda p, n=e['name']: prog(f"Giano: aggiorno «{n}» — {p}%"))
                    register(fid, r["path"], e["version"], sha, cur)
                    srv.agent("synced", {"file_id": int(fid), "version": e["version"]})
                    res["downloaded"] += 1
                    activity("scarico", f"Scaricata la v{e['version']} di «{e['name']}»", fid=int(fid))
                elif e.get("mac_version") is None and local_same:
                    srv.agent("synced", {"file_id": int(fid), "version": r["version"]})
            else:
                if e.get("request") in ("riscarica", "porta", "aggiorna") or e.get("mac_version") is not None:
                    if os.path.exists(target) and e.get("mac_version") is not None and _sha(target) == e["sha256"]:
                        register(fid, target_rel, e["version"], e["sha256"], target)
                        continue
                    if e.get("request"):
                        dest = _free_path(target)
                        prog(f"Giano: porto sul Mac «{e['name']}»")
                        sha = srv.download(int(fid), dest, lambda p, n=e['name']: prog(f"Giano: porto sul Mac «{n}» — {p}%"))
                        register(fid, _rel(root, dest), e["version"], sha, dest)
                        srv.agent("synced", {"file_id": int(fid), "version": e["version"]})
                        res["downloaded"] += 1
                        activity("scarico", f"Portato sul Mac «{e['name']}» → {e.get('dir') or 'cartella principale'}", fid=int(fid))
                    else:
                        srv.agent("deleted", {"file_id": int(fid)})   # Giano crede che il Mac l'abbia, ma non c'è
        except Exception as ex:
            log.warning("allineamento di %s non riuscito: %s", fid, ex)
            activity("errore", f"«{e.get('name', fid)}»: allineamento non riuscito ({ex})", ok=False, fid=int(fid))
            res["errors"] += 1
    save_registry(reg)

    # ── 3b. riconoscimento del testo delle scansioni (Vision di macOS)
    for job in data.get("ocr", []):
        import tempfile
        tmpd = tempfile.mkdtemp(prefix="giano-ocr-")
        try:
            prog(f"Giano: leggo il testo di «{job['name']}»")
            src, dst = os.path.join(tmpd, "in.pdf"), os.path.join(tmpd, "out.pdf")
            srv.download(int(job["id"]), src)
            from .giano_ocr import ocr_pdf, OcrError
            try:
                pages = ocr_pdf(src, dst)
            except OcrError as ex:
                srv.agent("ocr_failed", {"file_id": job["id"], "error": str(ex)[:200]})
                continue
            if not any(p.strip() for p in pages):
                srv.agent("ocr_failed", {"file_id": job["id"], "error": "nessun testo riconosciuto: le pagine sono vuote o troppo scure"})
                continue
            up = srv.upload(dst, job["name"], {"as_pending": True},
                            on_progress=lambda done, tot, n=job["name"]: prog(f"Giano: «{n}» reso ricercabile, invio — {int(done * 100 / max(1, tot))}%"))
            if up.get("status") != "in_sospeso":
                raise RuntimeError(up.get("error") or "invio non riuscito")
            srv.agent("ocr_done", {"file_id": job["id"], "sha256": up["sha256"], "size": up["size"], "pages": pages})
            res["ocr"] = res.get("ocr", 0) + 1
            activity("ocr", f"Letto il testo di «{job['name']}» ({len(pages)} pagine): ora è ricercabile", fid=int(job["id"]))
        except Exception as ex:
            log.warning("riconoscimento di %s non riuscito: %s", job.get("id"), ex)
            res["errors"] += 1
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)

    # ── 4. la cartella del Mac
    by_path = {r["path"]: fid for fid, r in F.items()}
    seen = set()
    for path in _iter_local(root):
        rel = _rel(root, path)
        if rel.startswith(TRASH_DIR + "/"):
            continue
        try:
            st = os.stat(path)
            if time.time() - st.st_mtime < 5:      # si sta ancora scrivendo
                seen.add(by_path.get(rel)); continue
            fid = by_path.get(rel)
            if fid:
                seen.add(fid)
                r = F[fid]
                if st.st_size == r.get("size") and abs(st.st_mtime - r.get("mtime", 0)) < 1e-3:
                    continue
                sha = _sha(path)
                if sha == r["sha"]:
                    r["size"], r["mtime"] = st.st_size, st.st_mtime
                    continue
                if sha in (r.get("pending_sha"), r.get("ignored_sha")) or open_pending.get(fid) == sha:
                    continue
                prog(f"Giano: «{os.path.basename(path)}» è cambiato sul Mac")
                up = srv.upload(path, os.path.basename(path), {"as_pending": True},
                                on_progress=lambda done, tot, n=os.path.basename(path): prog(f"Giano: «{n}» cambiato sul Mac, invio — {int(done * 100 / max(1, tot))}%"))
                if up.get("status") != "in_sospeso":
                    raise RuntimeError(up.get("error") or "invio in sospeso non riuscito")
                ans = srv.agent("changed", {"file_id": int(fid), "base_version": r["version"], "sha256": sha, "size": st.st_size})
                if ans.get("same"):
                    r["sha"] = sha
                elif ans.get("applied") == "aggiorna" and ans.get("version"):
                    r["sha"], r["version"] = sha, int(ans["version"])     # «Modifica sul Mac» o scelta ricordata
                    r["size"], r["mtime"] = st.st_size, st.st_mtime
                else:
                    r["pending_sha"] = sha
                    activity("modifica", f"«{os.path.basename(path)}» è cambiato sul Mac: aspetta una decisione", fid=int(fid))
                res["changed"] += 1
                continue
            # file nuovo
            if reg["skipped"].get(rel) == st.st_size:
                continue
            why = excluded(rel, st.st_size, g)
            if why:
                if reg.setdefault("excluded", {}).get(rel) != why:
                    reg["excluded"][rel] = why
                    activity("comando", f"«{os.path.basename(rel)}» non sale a Giano: {why}")
                continue
            reg.get("excluded", {}).pop(rel, None)
            if st.st_size > srv.max_file:
                reg["skipped"][rel] = st.st_size
                activity("errore", f"«{os.path.basename(rel)}» è troppo grande per Giano: resta solo sul Mac", ok=False)
                continue
            prog(f"Giano: invio «{os.path.basename(path)}»")
            sha = _sha(path)
            from .progress import fmt_size
            up = srv.upload(path, rel, on_progress=lambda done, tot, n=os.path.basename(path): prog(f"Giano: invio «{n}» — {int(done * 100 / max(1, tot))}% di {fmt_size(tot)}"))
            if up.get("status") == "rifiutato":
                reg["skipped"][rel] = st.st_size
                continue
            if up.get("status") == "doppione":
                ex = up.get("existing") or {}
                exid = str(ex.get("id") or "")
                if exid and exid not in F and srv.agent("adopt", {"file_id": int(exid), "sha256": sha}).get("ok"):
                    register(exid, rel, ex.get("version", 1), sha, path)
                    seen.add(exid)
                    continue
                up = srv.finish(up["upload_id"], {"force": True})
            if up.get("status") == "stesso_nome":
                up = srv.finish(up["upload_id"], {"on_same_name": "tieni_entrambi"})
            if up.get("status") != "fatto":
                raise RuntimeError(up.get("error") or f"stato {up.get('status')}")
            f = up["file"]
            register(f["id"], rel, f["version"], sha, path)
            seen.add(str(f["id"]))
            srv.agent("synced", {"file_id": f["id"], "version": f["version"]})
            srv.agent("log", {"orig_name": os.path.basename(path), "name": f["name"], "folder_id": f.get("folder_id")})
            activity("invio", f"Salito «{f['name']}»" + (f" → {os.path.dirname(rel)}" if os.path.dirname(rel) else ""), fid=int(f["id"]))
            try:
                p = srv.agent("process", {"file_id": f["id"]})
                if p.get("needs_text"):
                    # se il PDF non ha testo (una scansione), il server lo mette in coda per il riconoscimento
                    srv.agent("text", {"file_id": f["id"], "text": _pdf_text(path)})
            except Exception as ex:
                log.debug("ordine di %s non riuscito: %s", f["id"], ex)
            res["sent"] += 1
        except Exception as ex:
            log.warning("file %s: %s", rel, ex)
            activity("errore", f"«{os.path.basename(rel)}»: invio non riuscito ({ex})", ok=False)
            res["errors"] += 1
        save_registry(reg)

    # cancellati sul Mac
    for fid in [k for k in list(F.keys()) if k not in seen]:
        if os.path.exists(path_of(fid)):
            continue
        try:
            srv.agent("deleted", {"file_id": int(fid)})
            activity("cestino", f"«{os.path.basename(F[fid]['path'])}» cancellato sul Mac: in Giano resta «solo online»", fid=int(fid))
            del F[fid]
            res["deleted"] += 1
        except Exception as ex:
            log.warning("cancellazione di %s non segnalata: %s", fid, ex)
            res["errors"] += 1
    save_registry(reg)
    prog(f"Giano: {res['sent']} inviati, {res['downloaded']} scaricati, {res['changed']} da decidere")
    return res, srv
