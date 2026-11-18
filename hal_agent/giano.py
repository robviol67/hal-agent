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
                rr = self.c.post(u, params={"action": "chunk", "upload_id": uid, "offset": off}, content=piece,
                                 headers={**self.h, "Content-Type": "application/octet-stream"})
                if rr.status_code == 409:
                    off = int((rr.json() or {}).get("received", off))
                    continue
                rr.raise_for_status()
                off = int(rr.json()["received"])
                if on_progress:
                    on_progress(off, size)
        f = self.c.post(u + "?action=finish", json={"upload_id": uid, **(extra_finish or {})})
        f.raise_for_status()
        out = f.json()
        out["upload_id"] = uid
        return out

    def finish(self, upload_id: str, extra: dict) -> dict:
        f = self.c.post(f"{self.base}/api/giano/upload.php?action=finish", json={"upload_id": upload_id, **extra})
        f.raise_for_status()
        return f.json()

    def download(self, file_id: int, dest: str) -> str:
        """Scarica la versione corrente in dest (prima in un file temporaneo). Ritorna lo sha256."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".giano-tmp"
        h = hashlib.sha256()
        with self.c.stream("GET", f"{self.base}/api/giano/file.php", params={"id": file_id, "dl": 1}) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for b in r.iter_bytes(1 << 20):
                    fh.write(b)
                    h.update(b)
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
    conf = conf or cfg.load_config()
    g = conf.get("giano", {}) or {}
    res = {"ok": True, "sent": 0, "downloaded": 0, "moved": 0, "deleted": 0, "changed": 0, "errors": 0}

    def prog(m):
        if on_progress:
            on_progress(m)

    token, base = conf.get("token") or "", conf.get("server_url") or ""
    if not token or not base:
        res.update(ok=False, error_msg="collegamento non configurato")
        return res
    root = os.path.expanduser(g.get("folder") or str(Path.home() / "Giano"))
    os.makedirs(root, exist_ok=True)
    srv = Server(base, token)
    reg = load_registry()
    F = reg["files"]

    try:
        h = srv.agent("hello", {"folder": root.replace(str(Path.home()), "~"), "version": _version()})
        srv.chunk = int(h.get("chunk") or srv.chunk)
        srv.max_file = int(h.get("max_file") or srv.max_file)
        data = srv.agent("sync")
    except Exception as e:
        res.update(ok=False, error_msg=str(e)[:200])
        return res
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
        except Exception as e:
            log.warning("decisione %s non eseguita: %s", d.get("id"), e)
            res["errors"] += 1
    save_registry(reg)

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
                continue
            if fid in F:
                r = F[fid]
                cur = path_of(fid)
                if os.path.exists(cur) and r["path"] != target_rel:
                    if not os.path.exists(target):
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.move(cur, target)
                        r["path"] = target_rel
                        cur = target
                        res["moved"] += 1
                if not os.path.exists(cur):
                    if e.get("request") in ("riscarica", "porta", "aggiorna"):
                        prog(f"Giano: scarico «{e['name']}»")
                        sha = srv.download(int(fid), target)
                        register(fid, target_rel, e["version"], sha, target)
                        srv.agent("synced", {"file_id": int(fid), "version": e["version"]})
                        res["downloaded"] += 1
                    continue
                local_same = False
                st = os.stat(cur)
                if st.st_size == r.get("size") and abs(st.st_mtime - r.get("mtime", 0)) < 1e-3:
                    local_same = True
                elif _sha(cur) == r["sha"]:
                    local_same = True
                if (e["version"] > r["version"] and local_same) or e.get("request") == "aggiorna":
                    prog(f"Giano: aggiorno «{e['name']}»")
                    sha = srv.download(int(fid), cur)
                    register(fid, r["path"], e["version"], sha, cur)
                    srv.agent("synced", {"file_id": int(fid), "version": e["version"]})
                    res["downloaded"] += 1
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
                        sha = srv.download(int(fid), dest)
                        register(fid, _rel(root, dest), e["version"], sha, dest)
                        srv.agent("synced", {"file_id": int(fid), "version": e["version"]})
                        res["downloaded"] += 1
                    else:
                        srv.agent("deleted", {"file_id": int(fid)})   # Giano crede che il Mac l'abbia, ma non c'è
        except Exception as ex:
            log.warning("allineamento di %s non riuscito: %s", fid, ex)
            res["errors"] += 1
    save_registry(reg)

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
                up = srv.upload(path, os.path.basename(path), {"as_pending": True})
                if up.get("status") != "in_sospeso":
                    raise RuntimeError(up.get("error") or "invio in sospeso non riuscito")
                ans = srv.agent("changed", {"file_id": int(fid), "base_version": r["version"], "sha256": sha, "size": st.st_size})
                if ans.get("same"):
                    r["sha"] = sha
                else:
                    r["pending_sha"] = sha
                res["changed"] += 1
                continue
            # file nuovo
            if reg["skipped"].get(rel) == st.st_size:
                continue
            if st.st_size > srv.max_file:
                reg["skipped"][rel] = st.st_size
                continue
            prog(f"Giano: invio «{os.path.basename(path)}»")
            sha = _sha(path)
            up = srv.upload(path, rel)
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
            try:
                p = srv.agent("process", {"file_id": f["id"]})
                if p.get("needs_text"):
                    t = _pdf_text(path)
                    if t:
                        srv.agent("text", {"file_id": f["id"], "text": t})
            except Exception as ex:
                log.debug("ordine di %s non riuscito: %s", f["id"], ex)
            res["sent"] += 1
        except Exception as ex:
            log.warning("file %s: %s", rel, ex)
            res["errors"] += 1
        save_registry(reg)

    # cancellati sul Mac
    for fid in [k for k in list(F.keys()) if k not in seen]:
        if os.path.exists(path_of(fid)):
            continue
        try:
            srv.agent("deleted", {"file_id": int(fid)})
            del F[fid]
            res["deleted"] += 1
        except Exception as ex:
            log.warning("cancellazione di %s non segnalata: %s", fid, ex)
            res["errors"] += 1
    save_registry(reg)
    prog(f"Giano: {res['sent']} inviati, {res['downloaded']} scaricati, {res['changed']} da decidere")
    return res
