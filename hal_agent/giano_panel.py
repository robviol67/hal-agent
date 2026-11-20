"""
La scheda «Giano» del pannello (v0.9.3): la metà Mac di Giano, vista da vicino.

Legge i file che il giro scrive in ~/.hal-agent/ (vedi giano.py):
  giano_stato.json      situazione, ultimo giro, spazio online e sul Mac
  giano_web.json        TUTTI i file di Giano (anche solo online) e le decisioni aperte
  giano_registro.json   cosa c'è sul Mac
  giano_attivita.jsonl  lo storico
e lascia al giro i comandi (giano_comandi/): allinea, elenco, porta:<id>,
riscarica:<id>, svuota_cestino. Il pannello non parla mai col server da solo.
"""
import datetime
import logging
import os
import subprocess
import time
import webbrowser

from . import config as cfg
from . import giano
from .progress import fmt_size

log = logging.getLogger("hal_agent.giano_panel")

KINDS = {"pdf": "PDF", "documento": "Word e documenti", "foglio": "Fogli", "presentazione": "Presentazioni",
         "immagine": "Immagini", "scansione": "Scansioni", "testo": "Testi", "pagina_web": "Pagine web",
         "audio": "Audio", "video": "Video", "archivio": "Archivi", "altro": "Altro"}
PREFS = cfg.CONFIG_DIR / "giano_pannello.json"   # Elenco/Albero e cartelle aperte
FILTERS = ["Tutti", "Sul Mac", "Solo online", "In attesa di salire", "Il Mac è indietro", "Da decidere", "In coda per il Mac", "Errore", "Escluso"]
LOCAL_EVERY = 4        # secondi fra due letture della cartella del Mac
ACT_ICON = {"invio": "↑", "scarico": "↓", "sposto": "→", "cestino": "🗑", "modifica": "✎", "decisione": "✓",
            "ocr": "Aa", "errore": "!", "comando": "›"}


def _size(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f} GB".replace(".", ",")
    return fmt_size(n) if n else "0 KB"


def _hm(ts) -> str:
    if not ts:
        return "—"
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    if d.date() == today:
        return d.strftime("oggi %H:%M")
    if d.date() == today - datetime.timedelta(days=1):
        return d.strftime("ieri %H:%M")
    return d.strftime("%d/%m %H:%M")


def _mtime(p) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def build_giano_tab(nb, root):
    """Crea la scheda. Ritorna (frame, refresh, is_enabled)."""
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    tab = ttk.Frame(nb, padding=10)
    st_style = ttk.Style()
    for name, color in (("GOk", "#245f3f"), ("GWarn", "#8c4f14"), ("GErr", "#a3241b"), ("GRun", "#3d48a8"), ("GOff", "#777")):
        try:
            st_style.configure(name + ".TLabel", foreground=color, font=("TkDefaultFont", 13, "bold"))
        except Exception:
            pass

    # ── testata: situazione e pulsanti ─────────────────────────────────────
    top = ttk.Frame(tab)
    top.pack(fill="x")
    lbl_state = ttk.Label(top, text="—", style="GOff.TLabel")
    lbl_state.grid(column=0, row=0, sticky="w")
    lbl_detail = ttk.Label(top, text="", style="Muted.TLabel", wraplength=860, justify="left")
    lbl_detail.grid(column=0, row=1, columnspan=2, sticky="w", pady=(2, 4))
    btns = ttk.Frame(top)
    btns.grid(column=1, row=0, sticky="e")
    top.columnconfigure(0, weight=1)

    def conf_giano():
        return cfg.load_config().get("giano", {}) or {}

    def folder():
        return os.path.expanduser(conf_giano().get("folder") or "~/Giano")

    def server():
        return (cfg.load_config().get("server_url") or "https://hal-ai.it").rstrip("/")

    def on_sync():
        giano.send_command("allinea")
        flash("Allineamento chiesto: parte entro pochi secondi.")

    def on_pause():
        c = cfg.load_config()
        g = c.setdefault("giano", {})
        g["paused"] = not g.get("paused")
        cfg.save_config(c)
        flash("Giano in pausa: niente giri automatici («Allinea ora» funziona lo stesso)." if g["paused"] else "Giano riprende i giri.")
        redraw(force=True)

    def on_enable():
        c = cfg.load_config()
        g = c.setdefault("giano", {})
        g["enabled"] = True
        g["paused"] = False
        cfg.save_config(c)
        os.makedirs(folder(), exist_ok=True)
        giano.send_command("allinea")
        flash("Giano acceso: il primo giro parte fra pochi secondi.")
        redraw(force=True)

    b_sync = ttk.Button(btns, text="Allinea ora", command=on_sync)
    b_pause = ttk.Button(btns, text="Metti in pausa", command=on_pause)
    b_enable = ttk.Button(btns, text="Accendi Giano", command=on_enable)
    b_finder = ttk.Button(btns, text="Apri la cartella", command=lambda: subprocess.Popen(["open", folder()]))
    b_web = ttk.Button(btns, text="Apri Giano sul web", command=lambda: webbrowser.open(server() + "/giano/"))
    b_decide = ttk.Button(btns, text="Scegli…", command=lambda: show_decisions())
    for b in (b_sync, b_pause, b_finder, b_web):
        b.pack(side="left", padx=(6, 0))

    from . import updates
    upd = ttk.Frame(tab)
    lbl_upd = ttk.Label(upd, text="", foreground="#3d48a8")
    lbl_upd.pack(side="left")
    ttk.Button(upd, text="Scarica la versione nuova", command=lambda: webbrowser.open(updates.RELEASES_URL)).pack(side="left", padx=(10, 0))
    lbl_flash = ttk.Label(tab, text="", style="Muted.TLabel")
    lbl_flash.pack(fill="x")
    flash_until = [0.0]

    def flash(msg):
        lbl_flash.config(text=msg)
        flash_until[0] = time.time() + 6

    inner = ttk.Notebook(tab)
    inner.pack(fill="both", expand=True, pady=(4, 0))

    # ═══ FILE ═══════════════════════════════════════════════════════════════
    tf = ttk.Frame(inner, padding=8)
    inner.add(tf, text="File")
    bar = ttk.Frame(tf)
    bar.pack(fill="x")
    lbl_counts = ttk.Label(bar, text="", style="Muted.TLabel")
    lbl_counts.pack(side="left")
    v_search = tk.StringVar()
    v_filter = tk.StringVar(value="Tutti")
    ttk.Button(bar, text="Aggiorna l'elenco", command=lambda: (giano.send_command("elenco"), flash("Chiedo a Giano l'elenco aggiornato…"))).pack(side="right")
    bar2 = ttk.Frame(tf)
    bar2.pack(fill="x", pady=(6, 0))
    ttk.Label(bar2, text="Mostra").pack(side="left")
    cb = ttk.Combobox(bar2, textvariable=v_filter, values=FILTERS, state="readonly", width=20)
    cb.pack(side="left", padx=(6, 16))
    ttk.Label(bar2, text="Cerca").pack(side="left")
    e_search = ttk.Entry(bar2, textvariable=v_search, width=30)
    e_search.pack(side="left", padx=(6, 0))
    # Elenco / Albero (v0.9.4): la scelta resta in ~/.hal-agent/giano_pannello.json
    prefs = giano.read_json(PREFS, {})
    v_view = tk.StringVar(value=prefs.get("view") or "elenco")
    open_dirs = set(prefs.get("open") or [])

    def save_prefs():
        try:
            giano.write_json(PREFS, {"view": v_view.get(), "open": sorted(open_dirs)[:500]})
        except Exception:
            pass
    vseg = ttk.Frame(bar2)
    vseg.pack(side="right")
    ttk.Radiobutton(vseg, text="Elenco", value="elenco", variable=v_view, command=lambda: (save_prefs(), fill_files(force=True))).pack(side="left")
    ttk.Radiobutton(vseg, text="Albero", value="albero", variable=v_view, command=lambda: (save_prefs(), fill_files(force=True))).pack(side="left", padx=(8, 0))

    cols = ("nome", "cartella", "dim", "giano", "mac", "stato")
    heads = {"nome": "Nome", "cartella": "Cartella", "dim": "Dimensione", "giano": "Giano", "mac": "Mac", "stato": "Stato"}
    widths = {"nome": 230, "cartella": 170, "dim": 80, "giano": 50, "mac": 50, "stato": 190}
    tvw = ttk.Frame(tf)
    tvw.pack(fill="both", expand=True, pady=(6, 4))
    tv = ttk.Treeview(tvw, columns=cols, show="headings", height=12, selectmode="browse")
    sb = ttk.Scrollbar(tvw, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    sort = {"col": "nome", "rev": False}

    def set_sort(c):
        sort["rev"] = (not sort["rev"]) if sort["col"] == c else False
        sort["col"] = c
        fill_files(force=True)
    for c in cols:
        tv.heading(c, text=heads[c], command=lambda c=c: set_sort(c))
        tv.column(c, width=widths[c], anchor="w" if c in ("nome", "cartella", "stato") else "e", stretch=c in ("nome", "cartella"))
    tv.heading("#0", text="Nome", command=lambda: set_sort("nome"))
    tv.column("#0", width=380, stretch=True)
    for tag, color in (("decidi", "#8c4f14"), ("errore", "#a3241b"), ("online", "#6f675c"), ("indietro", "#3d48a8")):
        tv.tag_configure(tag, foreground=color)
    try:
        import tkinter.font as tkfont
        _f = tkfont.nametofont("TkDefaultFont").actual()
        tv.tag_configure("dir", font=(_f["family"], _f["size"], "bold"))
    except Exception:
        pass

    acts = ttk.Frame(tf)
    acts.pack(fill="x")
    b_porta = ttk.Button(acts, text="Porta sul Mac", width=18)
    b_riscarica = ttk.Button(acts, text="Riscarica")
    b_show = ttk.Button(acts, text="Mostra nel Finder")
    b_open = ttk.Button(acts, text="Apri in Giano")
    for b in (b_open, b_show, b_riscarica, b_porta):      # prima i pulsanti: l'etichetta prende lo spazio che resta
        b.pack(side="right", padx=(6, 0))
    lbl_sel = ttk.Label(acts, text="", style="Muted.TLabel")
    lbl_sel.pack(side="left", fill="x", expand=True)
    # le decisioni (v0.9.4): compaiono solo per un file «Da decidere»
    dec_bar = ttk.Frame(tf)
    b_ign = ttk.Button(dec_bar, text="Ignora")
    b_dup = ttk.Button(dec_bar, text="Duplica")
    b_agg = ttk.Button(dec_bar, text="Aggiorna")
    for b in (b_ign, b_dup, b_agg):
        b.pack(side="right", padx=(6, 0))
    lbl_dec = ttk.Label(dec_bar, text="", wraplength=520, justify="left")
    lbl_dec.pack(side="left", fill="x", expand=True)

    rows = {}           # iid → riga (file)
    dirs = {}           # iid → riepilogo della cartella (vista ad albero)
    data = {"web": None, "reg": None, "dec": set(), "sig": None}

    def compute():
        w = giano.read_json(giano.WEBLIST, {})
        reg = giano.load_registry().get("files", {})
        dec = {str(d.get("file_id")) for d in w.get("decisions", [])}
        base = folder()
        out = []
        for f in w.get("files", []):
            fid = str(f["id"])
            r = reg.get(fid)
            local = bool(r) and os.path.exists(os.path.join(base, r["path"]))
            if fid in dec or (r and r.get("pending_sha")):
                stato, tag = "Da decidere", "decidi"
            elif f.get("request_state") == "errore":
                stato, tag = "Errore", "errore"
            elif f.get("request") and f.get("request_state") in ("in_coda", "in_corso", "aspetta"):
                stato, tag = "In coda per il Mac", "indietro"
            elif local:
                stato, tag = ("Il Mac è indietro", "indietro") if int(f.get("version") or 0) > int(r.get("version") or 0) else ("Allineato", "")
            else:
                stato, tag = "Solo online", "online"
            out.append({"id": int(f["id"]), "name": f.get("name", ""), "dir": f.get("dir") or "", "size": int(f.get("size") or 0),
                        "v": int(f.get("version") or 0), "mv": int(r["version"]) if local else None, "stato": stato, "tag": tag,
                        "local": os.path.join(base, r["path"]) if local else None, "kind": f.get("kind"),
                        "can_decide": fid in dec})
        # v0.9.7: anche ciò che c'è SOLO sul Mac. L'elenco di Giano arriva a fine giro: senza
        # questo, una cartella appena incollata in ~/Giano non si vede finché il giro non finisce.
        seen = {str(f["id"]) for f in w.get("files", [])}
        known = set()
        for fid, r in reg.items():
            known.add(r["path"])
            if fid in seen:
                continue
            p_ = os.path.join(base, r["path"])
            if not os.path.exists(p_):
                continue
            d_, n_ = os.path.split(r["path"])
            out.append({"id": int(fid), "name": n_, "dir": d_, "size": int(r.get("size") or 0), "v": int(r.get("version") or 1),
                        "mv": int(r.get("version") or 1), "stato": "Appena salito", "tag": "", "local": p_,
                        "kind": "", "can_decide": False})
        step = (giano.read_json(giano.STATE, {}).get("step") or "") if giano.read_json(giano.STATE, {}).get("running") else ""
        g = conf_giano()
        for rel, size in local_files(base):
            if rel in known:
                continue
            d_, n_ = os.path.split(rel)
            why = giano.excluded(rel, size, g)
            if why:
                stato, tag = "Escluso", "online"
            elif f"«{n_}»" in step:
                stato, tag = "Sta salendo", "indietro"
            else:
                stato, tag = "In attesa di salire", "indietro"
            out.append({"id": None, "iid": "l:" + rel, "name": n_, "dir": d_, "size": size, "v": 0, "mv": None,
                        "stato": stato, "tag": tag, "local": os.path.join(base, rel), "kind": "", "can_decide": False,
                        "why": why})
        for r in out:
            r.setdefault("iid", str(r["id"]))
        data.update(web=w, rows=out, dec=dec)
        return out

    local_cache = {"at": 0.0, "root": "", "items": []}

    def local_files(base):
        """I file della cartella del Mac (come li vede il giro), riletti al più ogni LOCAL_EVERY secondi."""
        if local_cache["root"] == base and time.time() - local_cache["at"] < LOCAL_EVERY:
            return local_cache["items"]
        items = []
        if os.path.isdir(base):
            for p_ in giano._iter_local(base):
                rel = os.path.relpath(p_, base).replace(os.sep, "/")
                if rel.startswith(giano.TRASH_DIR + "/"):
                    continue
                try:
                    items.append((rel, os.path.getsize(p_)))
                except OSError:
                    pass
                if len(items) > 20000:
                    break
        local_cache.update(at=time.time(), root=base, items=items)
        return items

    def matches(r):
        flt, q = v_filter.get(), v_search.get().strip().lower()
        if q and q not in r["name"].lower() and q not in r["dir"].lower():
            return False
        if flt == "Tutti":
            return True
        if flt == "Sul Mac":
            return r["local"] is not None
        return r["stato"] == flt

    def file_values(r, with_dir=True):
        v = (r["dir"] or "—", _size(r["size"]), f"v{r['v']}", f"v{r['mv']}" if r["mv"] else "—", r["stato"])
        return ((r["name"],) + v) if with_dir else (("",) + v)

    def dir_summary(a):
        bits = [f"{a['n']} file"]
        if a["on"]:
            bits.append(f"{len(a['on'])} solo online")
        if a["dec"]:
            bits.append(f"{a['dec']} da decidere")
        if a["err"]:
            bits.append(f"{a['err']} errori")
        if a.get("up"):
            bits.append(f"{a['up']} da far salire")
        return " · ".join(bits)

    def fill_files(force=False):
        st_ = giano.read_json(giano.STATE, {})
        sig = (_mtime(giano.WEBLIST), _mtime(giano.REGISTRY), v_filter.get(), v_search.get(), sort["col"], sort["rev"], v_view.get(),
               int(time.time() // LOCAL_EVERY), st_.get("step") if st_.get("running") else "")
        if not force and sig == data["sig"]:
            return
        data["sig"] = sig
        allr = compute()
        w = data["web"] or {}
        if not allr and not w.get("at"):
            lbl_counts.config(text="L'elenco dei file arriva al prossimo giro (o con «Aggiorna l'elenco»).")
        else:
            n_mac = sum(1 for r in allr if r["local"])
            n_on = sum(1 for r in allr if r["stato"] == "Solo online")
            n_dec = sum(1 for r in allr if r["stato"] == "Da decidere")
            n_err = sum(1 for r in allr if r["stato"] == "Errore")
            n_up = sum(1 for r in allr if r["stato"] in ("In attesa di salire", "Sta salendo"))
            parts = [f"{len(allr)} file", f"{n_mac} sul Mac", f"{n_on} solo online"]
            if n_up:
                parts.append(f"{n_up} da far salire")
            if n_dec:
                parts.append(f"{n_dec} da decidere")
            if n_err:
                parts.append(f"{n_err} con errore")
            lbl_counts.config(text=" · ".join(parts) + f"   (elenco di {_hm(w.get('at'))})")
        keyf = {"nome": lambda r: r["name"].lower(), "cartella": lambda r: (r["dir"].lower(), r["name"].lower()), "dim": lambda r: r["size"],
                "giano": lambda r: r["v"], "mac": lambda r: r["mv"] or 0, "stato": lambda r: (r["stato"], r["name"].lower())}[sort["col"]]
        shown = sorted([r for r in allr if matches(r)], key=keyf, reverse=sort["rev"])
        sel = tv.selection()
        tv.delete(*tv.get_children())
        rows.clear()
        dirs.clear()
        if v_view.get() == "albero":
            tv.configure(show="tree headings", displaycolumns=("dim", "giano", "mac", "stato"))
            searching = bool(v_search.get().strip()) or v_filter.get() != "Tutti"
            agg = {}
            for r in shown:                      # riepilogo di ogni cartella (e delle sue antenate)
                parts_ = r["dir"].split("/") if r["dir"] else []
                for i in range(1, len(parts_) + 1):
                    a = agg.setdefault("/".join(parts_[:i]), {"n": 0, "size": 0, "on": [], "dec": 0, "err": 0, "up": 0})
                    a["n"] += 1
                    a["size"] += r["size"]
                    if r["stato"] == "Solo online":
                        a["on"].append(r["id"])
                    a["dec"] += r["stato"] == "Da decidere"
                    a["err"] += r["stato"] == "Errore"
                    a["up"] += r["stato"] in ("In attesa di salire", "Sta salendo")
            for path in sorted(agg, key=lambda p: [x.lower() for x in p.split("/")]):
                a = agg[path]
                parent = "d:" + path.rsplit("/", 1)[0] if "/" in path else ""
                iid = "d:" + path
                tag = "decidi" if a["dec"] else ("errore" if a["err"] else ("indietro" if a["up"] else "dir"))
                tv.insert(parent, "end", iid=iid, text="📁  " + path.rsplit("/", 1)[-1], open=searching or path in open_dirs,
                          tags=(tag, "dir") if tag != "dir" else ("dir",),
                          values=("", "", _size(a["size"]), "", "", dir_summary(a)))
                dirs[iid] = dict(a, path=path)
            for r in shown:
                iid = r["iid"]
                rows[iid] = r
                tv.insert("d:" + r["dir"] if r["dir"] else "", "end", iid=iid, text=r["name"],
                          tags=(r["tag"],) if r["tag"] else (), values=file_values(r, with_dir=False))
        else:
            tv.configure(show="headings", displaycolumns=cols)
            for r in shown[:5000]:
                iid = r["iid"]
                rows[iid] = r
                tv.insert("", "end", iid=iid, tags=(r["tag"],) if r["tag"] else (), values=file_values(r))
        if sel and (sel[0] in rows or sel[0] in dirs):
            tv.selection_set(sel[0])
            tv.see(sel[0])
        on_select()

    def on_open_close(opened):
        iid = tv.focus()
        if iid.startswith("d:"):
            (open_dirs.add if opened else open_dirs.discard)(iid[2:])
            save_prefs()
    tv.bind("<<TreeviewOpen>>", lambda e: on_open_close(True))
    tv.bind("<<TreeviewClose>>", lambda e: on_open_close(False))

    def current():
        s = tv.selection()
        return rows.get(s[0]) if s else None

    def current_dir():
        s = tv.selection()
        return dirs.get(s[0]) if s else None

    def on_select(_e=None):
        r, d = current(), current_dir()
        for b in (b_porta, b_riscarica, b_show, b_open):
            b.state(["disabled"])
        dec_bar.pack_forget()
        if d:
            b_porta.config(text=f"Porta sul Mac ({len(d['on'])})" if d["on"] else "Porta sul Mac")
            if d["on"]:
                b_porta.state(["!disabled"])
            if os.path.isdir(os.path.join(folder(), d["path"])):
                b_show.state(["!disabled"])
            lbl_sel.config(text=f"Cartella «{d['path']}» · {dir_summary(d)} · {_size(d['size'])}")
            return
        b_porta.config(text="Porta sul Mac")
        if not r:
            lbl_sel.config(text="Seleziona un file" + (" o una cartella." if v_view.get() == "albero" else "."))
            return
        if r["id"]:
            b_open.state(["!disabled"])
        if r["local"]:
            b_show.state(["!disabled"])
            if r["stato"] == "Il Mac è indietro":
                b_riscarica.state(["!disabled"])
        elif r["stato"] in ("Solo online", "Errore"):
            b_porta.state(["!disabled"])
        lbl_sel.config(text=f"{r['name']} · " + (KINDS.get(r['kind'], r['kind'] or '') if r["id"] else
                       ("non sale a Giano: " + r["why"] if r.get("why") else "sale a Giano con il giro" if r["stato"] != "Sta salendo" else "sta salendo adesso")))
        if r["stato"] == "Da decidere" and r["can_decide"]:
            lbl_dec.config(text=f"«{r['name']}» è cambiato sul Mac dopo l'invio. Aggiorna: la versione del Mac diventa quella nuova in Giano. "
                                "Duplica: tieni tutte e due, quella del Mac come copia. Ignora: resta quella di Giano e il Mac la riprende.")
            dec_bar.pack(fill="x", pady=(8, 0))

    def do_porta():
        r, d = current(), current_dir()
        if d and d["on"]:
            n = len(d["on"])
            if n > 20 and not messagebox.askyesno("Porta sul Mac", f"Porto sul Mac {n} file della cartella «{d['path']}» ({_size(sum(x['size'] for x in rows.values() if x['id'] in d['on']))})?", parent=root):
                return
            giano.send_command("porta", ",".join(str(i) for i in d["on"]))
            flash(f"{n} file della cartella «{d['path']}» scendono sul Mac al prossimo giro.")
            return
        if not r:
            return
        giano.send_command("porta", r["id"])
        flash(f"«{r['name']}» scende sul Mac al prossimo giro, in {r['dir'] or 'Giano'}.")

    def do_riscarica():
        r = current()
        if r:
            giano.send_command("riscarica", r["id"])
            flash(f"Riscarico «{r['name']}» al prossimo giro.")

    def do_show():
        r, d = current(), current_dir()
        if d:
            p_ = os.path.join(folder(), d["path"])
            if os.path.isdir(p_):
                subprocess.Popen(["open", p_])
        elif r and r["local"]:
            subprocess.Popen(["open", "-R", r["local"]])

    def do_open():
        r = current()
        if r and r["id"]:
            webbrowser.open(f"{server()}/giano/#/file/{r['id']}")

    def decide(choice):
        r = current()
        if not r:
            return
        giano.send_command("decidi", f"{r['id']}:{choice}")
        flash({"aggiorna": "Aggiorna", "duplica": "Duplica", "ignora": "Ignora"}[choice] + f": «{r['name']}» si sistema al prossimo giro (fra pochi secondi).")
        dec_bar.pack_forget()

    b_porta.config(command=do_porta)
    b_riscarica.config(command=do_riscarica)
    b_show.config(command=do_show)
    b_open.config(command=do_open)
    b_agg.config(command=lambda: decide("aggiorna"))
    b_dup.config(command=lambda: decide("duplica"))
    b_ign.config(command=lambda: decide("ignora"))
    tv.bind("<<TreeviewSelect>>", on_select)

    def on_double(_e=None):
        if current_dir():
            return               # sulla cartella il doppio clic la apre e la chiude
        do_show() if (current() or {}).get("local") else do_open()
    tv.bind("<Double-1>", on_double)
    cb.bind("<<ComboboxSelected>>", lambda e: fill_files(force=True))
    v_search.trace_add("write", lambda *a: fill_files(force=True))

    def show_decisions():
        inner.select(tf)
        v_filter.set("Da decidere")
        fill_files(force=True)

    # ═══ SPAZIO ═════════════════════════════════════════════════════════════
    ts = ttk.Frame(inner, padding=10)
    inner.add(ts, text="Spazio")
    lf_on = ttk.LabelFrame(ts, text="Online (hal-ai.it)", padding=10)
    lf_mac = ttk.LabelFrame(ts, text="Sul Mac", padding=10)
    lf_on.grid(column=0, row=0, sticky="nsew", padx=(0, 6))
    lf_mac.grid(column=1, row=0, sticky="nsew", padx=(6, 0))
    ts.columnconfigure(0, weight=1)
    ts.columnconfigure(1, weight=1)
    ts.rowconfigure(0, weight=1)
    on_lbls = ttk.Frame(lf_on)
    on_lbls.pack(fill="x")
    on_kinds = ttk.Frame(lf_on)
    on_kinds.pack(fill="both", expand=True, pady=(10, 0))
    mac_lbls = ttk.Frame(lf_mac)
    mac_lbls.pack(fill="x")
    disk_bar = ttk.Progressbar(lf_mac, maximum=100)
    disk_bar.pack(fill="x", pady=(8, 2))
    lbl_disk = ttk.Label(lf_mac, text="", style="Muted.TLabel")
    lbl_disk.pack(anchor="w")

    def on_empty_trash():
        s = giano.read_json(giano.STATE, {}).get("local") or {}
        if not s.get("trash_files"):
            messagebox.showinfo("Giano", "Il cestino locale è già vuoto.", parent=root)
            return
        if messagebox.askyesno("Svuota il cestino locale",
                               f"Nel cestino locale ci sono {s['trash_files']} file ({_size(s['trash_bytes'])}).\n\n"
                               "Sono file eliminati in Giano e tolti dalla cartella del Mac. Svuotandolo li cancelli "
                               "dal Mac per sempre (in Giano restano nel suo cestino finché non scade).\n\nSvuoto?",
                               icon="warning", parent=root):
            giano.send_command("svuota_cestino")
            flash("Svuoto il cestino locale al prossimo giro (fra pochi secondi).")
    b_trash = ttk.Button(lf_mac, text="Svuota il cestino locale…", command=on_empty_trash)
    b_trash.pack(anchor="w", pady=(10, 0))

    def on_check():
        if messagebox.askyesno("Controlla tutto",
                               "Rileggo tutti i file della cartella del Mac e li confronto con Giano, per scoprire file "
                               "cambiati senza che me ne accorgessi o rovinati. Con molti file ci vuole qualche minuto: "
                               "l'avanzamento lo vedi in alto.\n\nComincio?", parent=root):
            giano.send_command("controlla")
            flash("Controllo chiesto: parte con il prossimo giro (fra pochi secondi).")
    ttk.Button(lf_mac, text="Controlla tutti i file del Mac…", command=on_check).pack(anchor="w", pady=(6, 0))
    lbl_check = ttk.Label(lf_mac, text="", style="Muted.TLabel", wraplength=380, justify="left")
    lbl_check.pack(anchor="w", pady=(4, 0))
    lbl_space_note = ttk.Label(ts, text="", style="Muted.TLabel", wraplength=700, justify="left")
    lbl_space_note.grid(column=0, row=1, columnspan=2, sticky="w", pady=(8, 0))
    space_sig = [None]

    def kv(frame, rows_):
        for w_ in frame.winfo_children():
            w_.destroy()
        for i, (k, v) in enumerate(rows_):
            ttk.Label(frame, text=k).grid(column=0, row=i, sticky="w", pady=1)
            ttk.Label(frame, text=v, font=("TkDefaultFont", 12, "bold")).grid(column=1, row=i, sticky="e", pady=1, padx=(14, 0))
        frame.columnconfigure(0, weight=1)

    def fill_space(force=False):
        s = giano.read_json(giano.STATE, {})
        sig = (_mtime(giano.STATE), _mtime(giano.WEBLIST))
        if not force and sig == space_sig[0]:
            return
        space_sig[0] = sig
        on = s.get("online") or {}
        if on:
            kv(on_lbls, [("Spazio usato", _size(on.get("used_bytes"))), ("File", f"{int(on.get('files') or 0):,}".replace(",", ".")),
                         ("Versioni passate", _size(on.get("versions_bytes"))), ("Cestino di Giano", _size(on.get("trash_bytes"))),
                         ("In tutto", _size((on.get("used_bytes") or 0) + (on.get("versions_bytes") or 0) + (on.get("trash_bytes") or 0)))])
        else:
            kv(on_lbls, [("Arriva al prossimo giro", "")])
        for w_ in on_kinds.winfo_children():
            w_.destroy()
        kinds = on.get("by_kind") or []
        top = max([k["bytes"] for k in kinds] or [1]) or 1
        for i, k in enumerate(kinds[:8]):
            ttk.Label(on_kinds, text=KINDS.get(k["kind"], k["kind"])).grid(column=0, row=i, sticky="w")
            pb = ttk.Progressbar(on_kinds, maximum=top, value=k["bytes"], length=140)
            pb.grid(column=1, row=i, sticky="we", padx=8, pady=1)
            ttk.Label(on_kinds, text=_size(k["bytes"]), style="Muted.TLabel").grid(column=2, row=i, sticky="e")
        on_kinds.columnconfigure(1, weight=1)
        loc = s.get("local") or {}
        allr = data.get("rows") or []
        only_on = [r for r in allr if r["stato"] == "Solo online"]
        kv(mac_lbls, [("Cartella", (s.get("folder") or folder()).replace(os.path.expanduser("~"), "~")),
                      ("Nella cartella", f"{_size(loc.get('folder_bytes'))} · {loc.get('folder_files', 0)} file"),
                      ("Cestino locale", f"{_size(loc.get('trash_bytes'))} · {loc.get('trash_files', 0)} file"),
                      ("Solo online (non sul Mac)", f"{_size(sum(r['size'] for r in only_on))} · {len(only_on)} file")])
        tot, free = loc.get("disk_total") or 0, loc.get("disk_free") or 0
        if tot:
            disk_bar.config(value=round((tot - free) * 100 / tot))
            lbl_disk.config(text=f"Disco: liberi {_size(free)} su {_size(tot)}" + ("   ⚠ quasi pieno" if free < 5_000_000_000 else ""))
        chk = s.get("check") or {}
        lbl_check.config(text=(f"Ultimo controllo {_hm(chk.get('at'))}: {chk.get('n', 0)} file, "
                               + ("tutti uguali a Giano." if not chk.get("bad") else f"{chk['bad']} diversi (ripresi come «da decidere»).")) if chk else "Mai controllati.")
        lbl_space_note.config(text=("Spazio sul Mac misurato alla fine dell'ultimo giro (" + _hm(s.get("last_run")) + ")."
                                    if s.get("last_run") else "I numeri arrivano alla fine del primo giro."))

    # ═══ ATTIVITÀ ═══════════════════════════════════════════════════════════
    ta = ttk.Frame(inner, padding=8)
    inner.add(ta, text="Attività")
    abar = ttk.Frame(ta)
    abar.pack(fill="x")
    v_err = tk.BooleanVar(value=False)
    ttk.Checkbutton(abar, text="Solo gli errori", variable=v_err, command=lambda: fill_act(force=True)).pack(side="left")

    def export():
        p = filedialog.asksaveasfilename(parent=root, defaultextension=".txt", initialfile="giano-attivita.txt",
                                         filetypes=[("Testo", "*.txt")])
        if not p:
            return
        with open(p, "w", encoding="utf-8") as fh:
            for a in reversed(giano.read_activity(5000)):
                fh.write(f"{datetime.datetime.fromtimestamp(a.get('t', 0)):%Y-%m-%d %H:%M:%S}  {'OK ' if a.get('ok', True) else 'ERR'}  {a.get('x', '')}\n")
        flash("Registro esportato.")
    ttk.Button(abar, text="Esporta il registro…", command=export).pack(side="right")
    atw = ttk.Frame(ta)
    atw.pack(fill="both", expand=True, pady=(6, 0))
    tva = ttk.Treeview(atw, columns=("quando", "ic", "cosa"), show="headings", height=14)
    sba = ttk.Scrollbar(atw, orient="vertical", command=tva.yview)
    tva.configure(yscrollcommand=sba.set)
    tva.pack(side="left", fill="both", expand=True)
    sba.pack(side="right", fill="y")
    for c, t_, w_ in (("quando", "Quando", 100), ("ic", "", 30), ("cosa", "Che cosa", 560)):
        tva.heading(c, text=t_)
        tva.column(c, width=w_, anchor="w", stretch=c == "cosa")
    tva.tag_configure("err", foreground="#a3241b")
    act_sig = [None]

    def fill_act(force=False):
        sig = (_mtime(giano.ACTIVITY), v_err.get())
        if not force and sig == act_sig[0]:
            return
        act_sig[0] = sig
        tva.delete(*tva.get_children())
        items = giano.read_activity(500)
        if v_err.get():
            items = [a for a in items if not a.get("ok", True)]
        if not items:
            tva.insert("", "end", values=("", "", "Ancora niente: lo storico si riempie a ogni giro."))
        for a in items:
            tva.insert("", "end", tags=() if a.get("ok", True) else ("err",),
                       values=(_hm(a.get("t")), ACT_ICON.get(a.get("k"), "·"), a.get("x", "")))

    # ═══ IMPOSTAZIONI (v0.9.4) ═════════════════════════════════════════════
    ti = ttk.Frame(inner, padding=14)
    inner.add(ti, text="Impostazioni")
    EVERY = [(60, "ogni minuto"), (300, "ogni 5 minuti"), (900, "ogni 15 minuti"), (1800, "ogni 30 minuti"), (3600, "ogni ora")]
    g0 = conf_giano()
    v_every = tk.StringVar(value=next((l for s_, l in EVERY if s_ == int(g0.get("interval_seconds", 60))), "ogni minuto"))
    v_ext = tk.StringVar(value=", ".join(g0.get("exclude_ext") or []))
    v_dirs = tk.StringVar(value=", ".join(g0.get("exclude_dirs") or []))
    v_mb = tk.StringVar(value=str(g0.get("max_mb") or 0).rstrip("0").rstrip(".") if g0.get("max_mb") else "0")
    NOTIFY = [("importanti", "solo decisioni ed errori"), ("tutto", "anche invii e scaricamenti"), ("niente", "nessuna")]
    v_notify = tk.StringVar(value=next((l for k_, l in NOTIFY if k_ == (g0.get("notify") or "importanti")), NOTIFY[0][1]))
    r_ = 0
    for label, widget, help_ in (
        ("Un giro", ttk.Combobox(ti, textvariable=v_every, values=[l for _, l in EVERY], state="readonly", width=20),
         "Ogni quanto l'agente allinea la cartella con Giano."),
        ("Estensioni da non far salire", ttk.Entry(ti, textvariable=v_ext, width=44),
         "Separate da virgole, per esempio: dmg, iso, tmp. I file con queste estensioni restano solo sul Mac."),
        ("Cartelle da non far salire", ttk.Entry(ti, textvariable=v_dirs, width=44),
         "Nomi di cartelle (ovunque si trovino), oppure un percorso dentro Giano: node_modules, Foto grandi, Clienti/Archivio."),
        ("Limite di dimensione (MB)", ttk.Entry(ti, textvariable=v_mb, width=10),
         "I file più grandi restano solo sul Mac. 0 = nessun limite (vale comunque il limite di Giano)."),
        ("Notifiche", ttk.Combobox(ti, textvariable=v_notify, values=[l for _, l in NOTIFY], state="readonly", width=26),
         "Le notifiche di macOS: file da decidere, giri che non riescono, disco quasi pieno, esito di «Controlla tutto».")):
        ttk.Label(ti, text=label).grid(column=0, row=r_, sticky="w", pady=(6, 0))
        widget.grid(column=1, row=r_, sticky="w", pady=(6, 0), padx=(12, 0))
        ttk.Label(ti, text=help_, style="Muted.TLabel", wraplength=640, justify="left").grid(column=1, row=r_ + 1, sticky="w", padx=(12, 0))
        r_ += 2
    ttk.Label(ti, style="Muted.TLabel", wraplength=700, justify="left",
              text="Esclusioni e limite valgono per i file NUOVI: quelli già in Giano restano allineati.").grid(column=0, row=r_, columnspan=2, sticky="w", pady=(10, 0))

    def save_settings():
        try:
            mb = float((v_mb.get() or "0").replace(",", "."))
            if mb < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Giano", "Il limite di dimensione dev'essere un numero di MB (0 = nessun limite).", parent=root)
            return
        c = cfg.load_config()
        g = c.setdefault("giano", {})
        g["interval_seconds"] = next((s_ for s_, l in EVERY if l == v_every.get()), 60)
        g["exclude_ext"] = [x.strip().lower().lstrip(".") for x in v_ext.get().split(",") if x.strip()]
        g["exclude_dirs"] = [x.strip().strip("/") for x in v_dirs.get().split(",") if x.strip().strip("/")]
        g["max_mb"] = mb
        g["notify"] = next((k_ for k_, l in NOTIFY if l == v_notify.get()), "importanti")
        cfg.save_config(c)
        flash("Impostazioni di Giano salvate: valgono dal prossimo giro.")
    ttk.Button(ti, text="Salva", command=save_settings).grid(column=1, row=r_ + 1, sticky="w", padx=(12, 0), pady=(8, 0))

    # avvio all'accensione (v0.9.5): vale subito, senza «Salva»
    from . import autostart
    v_auto = tk.BooleanVar(value=autostart.is_enabled())
    lbl_auto = ttk.Label(ti, text="", style="Muted.TLabel", wraplength=560, justify="left")

    def on_auto():
        if v_auto.get():
            why = autostart.enable()
            if why:
                v_auto.set(False)
                lbl_auto.config(text="Non riesco: " + why + ".")
                return
            lbl_auto.config(text="Fatto: al prossimo accesso al Mac HAL Agent si apre da solo. macOS può mostrare l'avviso «elemento di login aggiunto».")
        else:
            autostart.disable()
            lbl_auto.config(text="Tolto: HAL Agent non si apre più da solo all'accensione.")
    cb_auto = ttk.Checkbutton(ti, text="Apri HAL Agent all'accensione del Mac", variable=v_auto, command=on_auto)
    cb_auto.grid(column=0, row=r_ + 2, columnspan=2, sticky="w", pady=(16, 0))
    lbl_auto.grid(column=0, row=r_ + 3, columnspan=2, sticky="w")
    if not autostart.app_path():
        cb_auto.state(["disabled"])
        lbl_auto.config(text="Disponibile con l'app installata in Applicazioni.")

    # ═══ testata ════════════════════════════════════════════════════════════
    head_sig = [None]

    def fill_head(force=False):
        g = conf_giano()
        s = giano.read_json(giano.STATE, {})
        now = time.time()
        if flash_until[0] and now > flash_until[0]:
            lbl_flash.config(text="")
            flash_until[0] = 0
        enabled, paused = bool(g.get("enabled")), bool(g.get("paused"))
        n_dec = sum(1 for r in (data.get("rows") or []) if r["stato"] == "Da decidere")
        n_up = sum(1 for r in (data.get("rows") or []) if r["stato"] in ("In attesa di salire", "Sta salendo"))
        if not enabled:
            lbl_state.config(text="○  Giano è spento su questo Mac", style="GOff.TLabel")
            lbl_detail.config(text="Accendilo per allineare la cartella " + folder().replace(os.path.expanduser("~"), "~") + " con Giano.")
        elif s.get("running") and now - s.get("started", now) < 3600:
            lbl_state.config(text="◐  Giro in corso", style="GRun.TLabel")
            lbl_detail.config(text=s.get("step") or "…")
        elif s.get("last_run") and not s.get("last_ok"):
            lbl_state.config(text="●  Errore nell'ultimo giro", style="GErr.TLabel")
            lbl_detail.config(text=(s.get("error") or "") + (f"  ·  {s.get('fails')} giri di fila" if (s.get("fails") or 0) > 1 else "") + "  ·  riprovo da solo")
        elif n_dec:
            lbl_state.config(text=f"●  {n_dec} file {'aspetta' if n_dec == 1 else 'aspettano'} una decisione", style="GWarn.TLabel")
            lbl_detail.config(text="Modificati sul Mac dopo l'invio: scegli qui (Aggiorna, Duplica o Ignora) nella scheda File.")
        elif n_up:
            lbl_state.config(text=f"◐  {n_up} file {'aspetta' if n_up == 1 else 'aspettano'} di salire", style="GRun.TLabel")
            lbl_detail.config(text="Sono nella cartella del Mac: salgono a Giano con il prossimo giro.")
        elif s.get("last_run"):
            lbl_state.config(text="●  Tutto allineato" + ("  ·  in pausa" if paused else ""), style="GWarn.TLabel" if paused else "GOk.TLabel")
            r = s.get("last_result") or {}
            bits = [f"{r[k]} {lab}" for k, lab in (("sent", "inviati"), ("downloaded", "scaricati"), ("moved", "spostati"), ("changed", "cambiati")) if r.get(k)]
            lbl_detail.config(text="")
        else:
            lbl_state.config(text="◐  In attesa del primo giro", style="GRun.TLabel")
            lbl_detail.config(text="")
        if enabled and s.get("last_run") and not s.get("running"):
            interval = max(20, int(g.get("interval_seconds", 60)))
            nxt = s["last_run"] + interval - now
            parts = [f"ultimo giro {_hm(s['last_run'])} ({s.get('duration', 0)} s)"]
            r = s.get("last_result") or {}
            bits = [f"{r[k]} {lab}" for k, lab in (("sent", "inviati"), ("downloaded", "scaricati"), ("moved", "spostati"), ("changed", "cambiati")) if r.get(k)]
            if bits:
                parts.append(", ".join(bits))
            parts.append("in pausa" if paused else ("prossimo fra pochi secondi" if nxt < 10 else f"prossimo fra {max(1, round(nxt / 60))} min"))
            if lbl_detail.cget("text") == "":
                lbl_detail.config(text="  ·  ".join(parts))
        nv = updates.newer()
        if nv:
            from . import __version__ as _v
            lbl_upd.config(text=f"È uscita la versione {nv} di HAL Agent (questa è la v{_v}).")
            if not upd.winfo_ismapped():
                upd.pack(fill="x", before=lbl_flash, pady=(0, 4))
        elif upd.winfo_ismapped():
            upd.pack_forget()
        # pulsanti
        for b in (b_enable, b_decide, b_sync, b_pause, b_finder, b_web):
            b.pack_forget()
        if enabled and n_dec:
            b_decide.pack(side="left", padx=(6, 0))
        if enabled:
            b_pause.config(text="Riprendi" if paused else "Metti in pausa")
            for b in (b_sync, b_pause, b_finder, b_web):
                b.pack(side="left", padx=(6, 0))
        else:
            for b in (b_enable, b_web):
                b.pack(side="left", padx=(6, 0))

    def refresh():
        fill_files()
        fill_space()
        fill_act()
        fill_head()

    def redraw(force=False):
        fill_files(force=force)
        fill_space(force=force)
        fill_act(force=force)
        fill_head(force=force)

    def is_enabled():
        return bool(conf_giano().get("enabled"))

    return tab, refresh, is_enabled
