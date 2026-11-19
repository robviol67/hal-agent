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
FILTERS = ["Tutti", "Sul Mac", "Solo online", "Il Mac è indietro", "Da decidere", "In coda per il Mac", "Errore"]
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
    for b in (b_sync, b_pause, b_finder, b_web):
        b.pack(side="left", padx=(6, 0))

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

    cols = ("nome", "cartella", "dim", "giano", "mac", "stato")
    heads = {"nome": "Nome", "cartella": "Cartella", "dim": "Dimensione", "giano": "Giano", "mac": "Mac", "stato": "Stato"}
    widths = {"nome": 230, "cartella": 170, "dim": 80, "giano": 50, "mac": 50, "stato": 140}
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
    for tag, color in (("decidi", "#8c4f14"), ("errore", "#a3241b"), ("online", "#6f675c"), ("indietro", "#3d48a8")):
        tv.tag_configure(tag, foreground=color)

    acts = ttk.Frame(tf)
    acts.pack(fill="x")
    lbl_sel = ttk.Label(acts, text="", style="Muted.TLabel")
    lbl_sel.pack(side="left")
    b_porta = ttk.Button(acts, text="Porta sul Mac")
    b_riscarica = ttk.Button(acts, text="Riscarica")
    b_show = ttk.Button(acts, text="Mostra nel Finder")
    b_open = ttk.Button(acts, text="Apri in Giano")
    for b in (b_open, b_show, b_riscarica, b_porta):
        b.pack(side="right", padx=(6, 0))

    rows = {}           # iid → riga
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
                        "local": os.path.join(base, r["path"]) if local else None, "kind": f.get("kind")})
        data.update(web=w, rows=out)
        return out

    def matches(r):
        flt, q = v_filter.get(), v_search.get().strip().lower()
        if q and q not in r["name"].lower() and q not in r["dir"].lower():
            return False
        if flt == "Tutti":
            return True
        if flt == "Sul Mac":
            return r["local"] is not None
        return r["stato"] == flt

    def fill_files(force=False):
        sig = (_mtime(giano.WEBLIST), _mtime(giano.REGISTRY), v_filter.get(), v_search.get(), sort["col"], sort["rev"])
        if not force and sig == data["sig"]:
            return
        data["sig"] = sig
        allr = compute()
        w = data["web"] or {}
        if not w.get("files") and not w.get("at"):
            lbl_counts.config(text="L'elenco dei file arriva al prossimo giro (o con «Aggiorna l'elenco»).")
        else:
            n_mac = sum(1 for r in allr if r["local"])
            n_on = sum(1 for r in allr if r["stato"] == "Solo online")
            n_dec = sum(1 for r in allr if r["stato"] == "Da decidere")
            n_err = sum(1 for r in allr if r["stato"] == "Errore")
            parts = [f"{len(allr)} file", f"{n_mac} sul Mac", f"{n_on} solo online"]
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
        for r in shown[:5000]:
            iid = str(r["id"])
            rows[iid] = r
            tv.insert("", "end", iid=iid, tags=(r["tag"],) if r["tag"] else (),
                      values=(r["name"], r["dir"] or "—", _size(r["size"]), f"v{r['v']}", f"v{r['mv']}" if r["mv"] else "—", r["stato"]))
        if sel and sel[0] in rows:
            tv.selection_set(sel[0])
        on_select()

    def current():
        s = tv.selection()
        return rows.get(s[0]) if s else None

    def on_select(_e=None):
        r = current()
        for b in (b_porta, b_riscarica, b_show, b_open):
            b.state(["disabled"])
        if not r:
            lbl_sel.config(text="Seleziona un file.")
            return
        b_open.state(["!disabled"])
        if r["local"]:
            b_show.state(["!disabled"])
            if r["stato"] == "Il Mac è indietro":
                b_riscarica.state(["!disabled"])
        elif r["stato"] in ("Solo online", "Errore"):
            b_porta.state(["!disabled"])
        lbl_sel.config(text=f"{r['name']} · {KINDS.get(r['kind'], r['kind'] or '')}")

    def do_porta():
        r = current()
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
        r = current()
        if r and r["local"]:
            subprocess.Popen(["open", "-R", r["local"]])

    def do_open():
        r = current()
        if r:
            webbrowser.open(f"{server()}/giano/#/file/{r['id']}")

    b_porta.config(command=do_porta)
    b_riscarica.config(command=do_riscarica)
    b_show.config(command=do_show)
    b_open.config(command=do_open)
    tv.bind("<<TreeviewSelect>>", on_select)
    tv.bind("<Double-1>", lambda e: (do_show() if (current() or {}).get("local") else do_open()))
    cb.bind("<<ComboboxSelected>>", lambda e: fill_files(force=True))
    v_search.trace_add("write", lambda *a: fill_files(force=True))

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
            lbl_detail.config(text="Modificati sul Mac dopo l'invio: scegli in Giano sul web se aggiornare, duplicare o ignorare.")
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
        # pulsanti
        for b in (b_enable, b_sync, b_pause, b_finder, b_web):
            b.pack_forget()
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
