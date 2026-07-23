"""
Pannello dell'agente HAL (Tkinter).

È la finestra che si apre dalla menu-bar: mostra QUALI Scout sono collegati,
QUANTI elementi hanno raccolto e inviato negli ultimi giri, cosa sta facendo il
Ponte LLM, e permette di configurare tutto senza toccare il file JSON.

Gira in un PROCESSO SEPARATO (comando `panel`), come la vecchia finestra del
ponte: Tk vuole il suo main-loop e non può convivere con quello di pystray.
Comunica col processo menu-bar attraverso i file in ~/.hal-agent/
(runtime.json per leggere lo stato, trigger per chiedere una raccolta subito).
"""
import datetime
import json
import logging
import subprocess
import sys
import threading

from . import __version__
from . import config as cfg
from . import remote
from . import telemetry
from .config_window import PRESETS, fetch_models, test_generation, _open_json_fallback

log = logging.getLogger("hal_agent.panel")

REFRESH_MS = 2000

INTERVAL_CHOICES = [(15, "ogni 15 minuti"), (30, "ogni 30 minuti"), (60, "ogni ora"),
                    (120, "ogni 2 ore"), (360, "ogni 6 ore"), (720, "ogni 12 ore")]

BRIDGE_HELP = (
    "A cosa serve il Ponte LLM\n"
    "\n"
    "Il sito HAL (hal-ai.it) sa scrivere e riassumere usando l'intelligenza "
    "artificiale. Di norma lo fa con i provider cloud configurati sul sito "
    "(Anthropic, Gemini, DeepSeek). Il Ponte serve SOLO se vuoi che quel lavoro "
    "lo faccia un modello che gira su questo computer (LM Studio, Ollama) o "
    "raggiungibile solo da qui: niente costi a token e i testi non escono dal Mac.\n"
    "\n"
    "Come funziona: quando è attivo, l'agente chiede al sito ogni pochi secondi "
    "«ci sono lavori per me?». Se il sito ne ha uno in coda lo esegue sul modello "
    "locale e rimanda indietro il testo. Non apre nessuna porta in ingresso: è "
    "sempre l'agente a chiamare fuori, quindi non serve alcun tunnel né toccare "
    "il router.\n"
    "\n"
    "Se sul sito usi solo provider cloud puoi lasciarlo spento: attivo e senza "
    "lavori in coda non fa nulla, se non un controllo periodico del modello locale."
)


# ─── formattazione ──────────────────────────────────────────────────────────
def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        d = datetime.datetime.fromtimestamp(float(ts))
    except Exception:
        return "—"
    today = datetime.date.today()
    if d.date() == today:
        return "oggi " + d.strftime("%H:%M")
    if d.date() == today - datetime.timedelta(days=1):
        return "ieri " + d.strftime("%H:%M")
    return d.strftime("%d/%m %H:%M")


def _fmt_countdown(ts) -> str:
    if not ts:
        return "non pianificata (agente non in esecuzione?)"
    secs = int(float(ts) - datetime.datetime.now().timestamp())
    if secs < -120:
        # scadenza superata da un pezzo: nessuno l'ha aggiornata, l'agente è giù
        return "in ritardo — l'agente nella barra sembra fermo"
    if secs <= 0:
        return "a momenti"
    if secs < 60:
        return "tra %ds" % secs
    if secs < 3600:
        return "tra %dm" % (secs // 60)
    return "tra %dh %dm" % (secs // 3600, (secs % 3600) // 60)


def _scout_sources(agent: dict):
    """Elenco leggibile delle fonti di uno Scout: [(tipo, indirizzo), …]."""
    out = []
    for u in agent.get("rss_feeds") or []:
        out.append(("RSS", u))
    for s in agent.get("reddit_subreddits") or []:
        name = str(s).strip().strip("/")
        if name.lower().startswith("r/"):
            name = name[2:]
        out.append(("Reddit", "r/" + name))
    for c in agent.get("youtube_channels") or []:
        out.append(("YouTube", c))
    return out


def _load_scouts():
    """
    Scout attualmente in carico all'agente, con la loro provenienza.
    Stessa precedenza del runner: server (cache su disco) prima del config locale.
    """
    cached = remote.load_cached_config()
    if cached and isinstance(cached.get("agents"), list):
        return cached["agents"], "dal sito HAL"
    conf = cfg.load_config()
    return conf.get("agents", []), "dal file locale (server mai raggiunto)"


def open_panel():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception as e:
        log.error("Tkinter non disponibile (%s): apro il file di configurazione", e)
        _open_json_fallback()
        return

    root = tk.Tk()
    root.title("HAL Agent — Pannello")
    root.geometry("820x560")
    root.minsize(720, 480)

    style = ttk.Style()
    try:
        style.configure("Muted.TLabel", foreground="#777")
        style.configure("Head.TLabel", font=("TkDefaultFont", 13, "bold"))
    except Exception:
        pass

    # ── intestazione ────────────────────────────────────────────────────────
    head = ttk.Frame(root, padding=(14, 10, 14, 4))
    head.pack(fill="x")
    ttk.Label(head, text="HAL Agent v%s" % __version__, style="Head.TLabel").grid(
        column=0, row=0, sticky="w")
    lbl_status = ttk.Label(head, text="—", style="Muted.TLabel")
    lbl_status.grid(column=0, row=1, sticky="w", pady=(2, 0))
    lbl_next = ttk.Label(head, text="", style="Muted.TLabel")
    lbl_next.grid(column=1, row=1, sticky="e")
    head.columnconfigure(1, weight=1)

    nb = ttk.Notebook(root, padding=(10, 6))
    nb.pack(fill="both", expand=True)

    # ═══ TAB 1 — SCOUT ══════════════════════════════════════════════════════
    tab_scout = ttk.Frame(nb, padding=10)
    nb.add(tab_scout, text="Scout collegati")

    bar = ttk.Frame(tab_scout)
    bar.pack(fill="x", pady=(0, 8))
    lbl_source = ttk.Label(bar, text="", style="Muted.TLabel")
    lbl_source.pack(side="left")
    ttk.Button(bar, text="Raccogli ora", command=lambda: on_run_now()).pack(side="right")
    ttk.Button(bar, text="Aggiorna dal sito",
               command=lambda: on_refresh_scouts()).pack(side="right", padx=6)

    cols = ("fonti", "raccolti", "nuovi", "inviati", "totale", "ultimo")
    tv_scout = ttk.Treeview(tab_scout, columns=cols, show="tree headings", height=8)
    tv_scout.heading("#0", text="Scout")
    tv_scout.column("#0", width=200, anchor="w")
    for key, label, width in (("fonti", "Fonti", 60), ("raccolti", "Raccolti", 80),
                              ("nuovi", "Nuovi", 70), ("inviati", "Inviati", 70),
                              ("totale", "Inviati in totale", 120),
                              ("ultimo", "Ultimo invio", 110)):
        tv_scout.heading(key, text=label)
        tv_scout.column(key, width=width, anchor="center")
    tv_scout.pack(fill="both", expand=True)
    ttk.Label(tab_scout, style="Muted.TLabel",
              text="«Raccolti / Nuovi / Inviati» si riferiscono all'ultimo giro di raccolta.")\
        .pack(anchor="w", pady=(4, 6))

    txt_scout = tk.Text(tab_scout, height=7, wrap="word", relief="flat",
                        background=root.cget("background"))
    txt_scout.pack(fill="x")
    txt_scout.configure(state="disabled")

    def _set_text(widget, content):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    scouts_data = {}   # iid -> agent dict
    # firme dell'ultimo disegno: ridisegniamo le tabelle solo se qualcosa è
    # cambiato, altrimenti ogni 2s perderemmo selezione e posizione di scorrimento.
    drawn = {"scouts": None, "runs": None}

    def on_select_scout(_e=None):
        sel = tv_scout.selection()
        if not sel:
            return
        agent = scouts_data.get(sel[0])
        if not agent:
            return
        kw = ", ".join(agent.get("keywords") or []) or "(nessuna: prende tutto)"
        stat = telemetry.read().get("scouts", {}).get(agent.get("name") or "(senza nome)", {})
        bad = {str(t): m for t, m in (stat.get("last_bad_sources") or [])}
        lines = ["Parole chiave: " + kw, ""]
        srcs = _scout_sources(agent)
        lines.append("Fonti (%d):" % len(srcs) if srcs else "Nessuna fonte configurata.")
        for kind, target in srcs:
            why = next((m for t, m in bad.items() if target in t or t in target), "")
            lines.append("  %s [%s] %s%s" % ("✘" if why else "•", kind, target,
                                             ("   → " + why) if why else ""))
        if stat.get("last_error"):
            lines += ["", "⚠ Ultimo errore: " + stat["last_error"]]
        _set_text(txt_scout, "\n".join(lines))

    tv_scout.bind("<<TreeviewSelect>>", on_select_scout)

    def fill_scouts():
        agents, source = _load_scouts()
        stats = telemetry.read().get("scouts", {})
        sig = json.dumps([source] + [[a.get("name"), len(_scout_sources(a)),
                                      stats.get(a.get("name") or "(senza nome)", {})]
                                     for a in agents], sort_keys=True, default=str)
        if sig == drawn["scouts"]:
            return
        drawn["scouts"] = sig
        keep = tv_scout.selection()
        tv_scout.delete(*tv_scout.get_children())
        scouts_data.clear()
        for i, a in enumerate(agents):
            name = a.get("name") or "(senza nome)"
            st = stats.get(name, {})
            iid = "s%d" % i
            tv_scout.insert("", "end", iid=iid, text=name, values=(
                len(_scout_sources(a)),
                st.get("last_raw", "—"), st.get("last_new", "—"), st.get("last_sent", "—"),
                st.get("total_sent", 0), _fmt_ts(st.get("last_sent_ts")),
            ))
            scouts_data[iid] = a
        lbl_source.config(text="%d Scout — elenco %s" % (len(agents), source))
        if keep and keep[0] in scouts_data:
            tv_scout.selection_set(keep[0])
        elif scouts_data:
            tv_scout.selection_set("s0")

    def on_refresh_scouts():
        conf = cfg.load_config()

        def work():
            ok, msg, _n = remote.probe(conf)

            def done():
                fill_scouts()
                (messagebox.showinfo if ok else messagebox.showwarning)("HAL Agent", msg)
            try:
                root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def on_run_now():
        telemetry.request_run()
        messagebox.showinfo("HAL Agent",
                            "Raccolta richiesta. Parte entro pochi secondi: i numeri "
                            "qui sotto si aggiornano da soli a fine giro.\n\n"
                            "(Se l'agente nella barra in alto non è in esecuzione, "
                            "la richiesta resta in attesa.)")

    # ═══ TAB 2 — INVII ══════════════════════════════════════════════════════
    tab_runs = ttk.Frame(nb, padding=10)
    nb.add(tab_runs, text="Invii recenti")

    lbl_runs_sum = ttk.Label(tab_runs, text="", style="Muted.TLabel")
    lbl_runs_sum.pack(anchor="w", pady=(0, 8))

    rcols = ("raccolti", "nuovi", "inviati", "esito")
    tv_runs = ttk.Treeview(tab_runs, columns=rcols, show="tree headings", height=9)
    tv_runs.heading("#0", text="Quando")
    tv_runs.column("#0", width=160, anchor="w")
    for key, label, width in (("raccolti", "Raccolti", 90), ("nuovi", "Nuovi", 80),
                              ("inviati", "Inviati", 80), ("esito", "Esito", 220)):
        tv_runs.heading(key, text=label)
        tv_runs.column(key, width=width, anchor="center")
    tv_runs.pack(fill="both", expand=True)

    txt_runs = tk.Text(tab_runs, height=8, wrap="word", relief="flat",
                       background=root.cget("background"))
    txt_runs.pack(fill="x", pady=(8, 0))
    txt_runs.configure(state="disabled")

    runs_data = {}

    def on_select_run(_e=None):
        sel = tv_runs.selection()
        if not sel:
            return
        run = runs_data.get(sel[0])
        if not run:
            return
        lines = ["Giro: %s — durata %ss — Scout %s" % (
            _fmt_ts(run.get("ts")), run.get("duration", "?"),
            "dal sito" if run.get("source") == "server"
            else ("dalla cache" if run.get("source") == "cache" else "dal file locale"))]
        if run.get("dry_run"):
            lines.append("(prova a vuoto: nessun invio reale)")
        if run.get("error"):
            lines.append("Errore: " + str(run["error"]))
        lines.append("")
        for s in run.get("scouts", []):
            if s.get("error"):
                lines.append("  ⚠ %s — errore: %s" % (s.get("name"), s["error"]))
            else:
                n = int(s.get("sources", 0))
                lines.append("  • %s — raccolti %d, nuovi %d, inviati %d (%d font%s)" % (
                    s.get("name"), s.get("raw", 0), s.get("new", 0),
                    s.get("sent", 0), n, "e" if n == 1 else "i"))
            for target, why in (s.get("bad_sources") or []):
                lines.append("      ✘ fonte muta: %s → %s" % (target, why))
        if not run.get("scouts"):
            lines.append("  (nessuno Scout in questo giro)")
        _set_text(txt_runs, "\n".join(lines))

    tv_runs.bind("<<TreeviewSelect>>", on_select_run)

    def fill_runs():
        data = telemetry.read()
        runs = list(reversed(data.get("runs", [])))
        sig = "%d|%s" % (len(runs), runs[0].get("ts") if runs else "")
        if sig == drawn["runs"]:
            return
        drawn["runs"] = sig
        keep = tv_runs.selection()
        tv_runs.delete(*tv_runs.get_children())
        runs_data.clear()
        for i, r in enumerate(runs):
            if r.get("error"):
                esito = "invio fallito"
            elif not r.get("ok"):
                esito = "errore"
            elif r.get("dry_run"):
                esito = "prova a vuoto"
            elif r.get("sent"):
                esito = "inviati al sito"
            else:
                esito = "niente di nuovo"
            iid = "r%d" % i
            tv_runs.insert("", "end", iid=iid, text=_fmt_ts(r.get("ts")),
                           values=(r.get("raw", 0), r.get("new", 0), r.get("sent", 0), esito))
            runs_data[iid] = r
        tot = sum(int(r.get("sent", 0)) for r in runs)
        if not runs:
            testo = "Nessun giro registrato finora."
        else:
            testo = ("%s: %d elementi inviati in totale. "
                     "Clicca un giro per vedere il dettaglio per Scout."
                     % ("Ultimo giro" if len(runs) == 1 else "Ultimi %d giri" % len(runs), tot))
        lbl_runs_sum.config(text=testo)
        if keep and keep[0] in runs_data:
            tv_runs.selection_set(keep[0])
        elif runs_data:
            tv_runs.selection_set("r0")

    # ═══ TAB 3 — PONTE LLM ══════════════════════════════════════════════════
    tab_br = ttk.Frame(nb, padding=10)
    nb.add(tab_br, text="Ponte LLM")

    left = ttk.Frame(tab_br)
    left.pack(side="left", fill="both", expand=True, padx=(0, 12))
    txt_help = tk.Text(left, wrap="word", relief="flat", height=18, width=44,
                       background=root.cget("background"))
    txt_help.pack(fill="both", expand=True)
    txt_help.insert("1.0", BRIDGE_HELP)
    txt_help.configure(state="disabled")

    right = ttk.Frame(tab_br)
    right.pack(side="left", fill="y")

    conf0 = cfg.load_config()
    br0 = dict(conf0.get("llm_bridge") or {})
    v_br_on = tk.BooleanVar(value=bool(br0.get("enabled")))
    v_preset = tk.StringVar()
    v_endpoint = tk.StringVar(value=br0.get("endpoint", ""))
    v_model = tk.StringVar(value=br0.get("model", ""))
    v_key = tk.StringVar(value=br0.get("api_key", ""))

    lbl_br_state = ttk.Label(right, text="—", wraplength=300, justify="left")
    lbl_br_state.grid(column=0, row=0, columnspan=2, sticky="w", pady=(0, 10))

    ttk.Checkbutton(right, text="Ponte LLM attivo", variable=v_br_on)\
        .grid(column=0, row=1, columnspan=2, sticky="w", pady=(0, 8))

    ttk.Label(right, text="Preset").grid(column=0, row=2, sticky="w")
    cb_preset = ttk.Combobox(right, textvariable=v_preset, values=list(PRESETS.keys()),
                             state="readonly", width=26)
    cb_preset.grid(column=1, row=2, sticky="we", pady=3)

    ttk.Label(right, text="Endpoint").grid(column=0, row=3, sticky="w")
    ttk.Entry(right, textvariable=v_endpoint, width=29).grid(column=1, row=3, sticky="we", pady=3)

    ttk.Label(right, text="Modello").grid(column=0, row=4, sticky="w")
    cb_model = ttk.Combobox(right, textvariable=v_model, width=26)
    cb_model.grid(column=1, row=4, sticky="we", pady=3)

    ttk.Label(right, text="API key").grid(column=0, row=5, sticky="w")
    ttk.Entry(right, textvariable=v_key, width=29, show="*").grid(column=1, row=5, sticky="we", pady=3)

    def on_preset(*_):
        p = PRESETS.get(v_preset.get())
        if p:
            v_endpoint.set(p["endpoint"])
            v_model.set(p["model"])
    cb_preset.bind("<<ComboboxSelected>>", on_preset)

    def detect_models():
        ep, key = v_endpoint.get().strip(), v_key.get().strip()
        btn_detect.config(state="disabled", text="Cerco…")

        def work():
            ids, err = fetch_models(ep, key)

            def done():
                btn_detect.config(state="normal", text="Rileva modelli")
                if ids:
                    cb_model["values"] = ids
                    if v_model.get() not in ids:
                        v_model.set(ids[0])
                    messagebox.showinfo("HAL Agent", "Trovati %d modelli." % len(ids))
                else:
                    messagebox.showwarning("HAL Agent", "Nessun modello rilevato.\n\n" + (err or ""))
            try:
                root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def test_bridge():
        ep, md, key = v_endpoint.get().strip(), v_model.get().strip(), v_key.get().strip()
        btn_test.config(state="disabled", text="Provo…")

        def work():
            ok, msg = test_generation(ep, md, key)

            def done():
                btn_test.config(state="normal", text="Prova il modello")
                if ok:
                    messagebox.showinfo("HAL Agent", "Il modello risponde:\n\n" + msg)
                else:
                    messagebox.showerror("HAL Agent", "Prova fallita:\n\n" + msg)
            try:
                root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def save_bridge():
        c = cfg.load_config()
        b = c.setdefault("llm_bridge", {})
        b["enabled"] = bool(v_br_on.get())
        b["endpoint"] = v_endpoint.get().strip()
        b["model"] = v_model.get().strip()
        b["api_key"] = v_key.get().strip()
        p = PRESETS.get(v_preset.get())
        if p:
            b["provider"] = p["provider"]
        b.setdefault("poll_path", "/api/agent/jobs")
        b.setdefault("result_path", "/api/agent/jobs/result")
        cfg.save_config(c)
        messagebox.showinfo("HAL Agent", "Impostazioni del ponte salvate. "
                                         "L'agente le applica al prossimo controllo (max 10 secondi).")

    btn_detect = ttk.Button(right, text="Rileva modelli", command=detect_models)
    btn_detect.grid(column=1, row=6, sticky="e", pady=(8, 0))
    btn_test = ttk.Button(right, text="Prova il modello", command=test_bridge)
    btn_test.grid(column=1, row=7, sticky="e", pady=4)
    ttk.Button(right, text="Salva", command=save_bridge).grid(column=1, row=8, sticky="e", pady=(8, 0))

    def refresh_bridge_state():
        br = telemetry.read().get("bridge", {}) or {}
        conf = cfg.load_config()
        on = bool((conf.get("llm_bridge") or {}).get("enabled"))
        if not on:
            txt = "Stato: SPENTO — il sito non può usare il modello locale."
        elif br.get("checked_ts") is None:
            txt = "Stato: ACCESO — in attesa del primo controllo…"
        elif br.get("ok"):
            txt = ("Stato: ACCESO e funzionante\nModello raggiungibile su %s\nUltimo controllo: %s"
                   % (br.get("endpoint") or "?", _fmt_ts(br.get("checked_ts"))))
        else:
            txt = ("Stato: ACCESO ma il modello NON risponde\n%s\n%s\nUltimo controllo: %s"
                   % (br.get("endpoint") or "?", br.get("detail") or "",
                      "Avvia LM Studio/Ollama o correggi l'endpoint.", _fmt_ts(br.get("checked_ts"))))
        jobs = int(br.get("jobs_done", 0))
        if jobs:
            last = br.get("last_job") or {}
            txt += "\n\nLavori eseguiti: %d (falliti %d)\nUltimo: %s — %s" % (
                jobs, int(br.get("jobs_failed", 0)), _fmt_ts(last.get("ts")),
                ("%d caratteri restituiti" % last.get("chars", 0)) if last.get("ok")
                else (last.get("detail") or "fallito"))
        else:
            txt += "\n\nNessun lavoro ricevuto dal sito finora."
        lbl_br_state.config(text=txt)

    # ═══ TAB 4 — COLLEGAMENTO / IMPOSTAZIONI ════════════════════════════════
    tab_cfg = ttk.Frame(nb, padding=14)
    nb.add(tab_cfg, text="Collegamento")

    v_server = tk.StringVar(value=conf0.get("server_url", ""))
    v_token = tk.StringVar(value=conf0.get("token", ""))
    v_show_token = tk.BooleanVar(value=False)
    v_interval = tk.StringVar()
    v_days = tk.StringVar(value=str(conf0.get("days_limit", 7)))

    _int_labels = {mins: lab for mins, lab in INTERVAL_CHOICES}
    v_interval.set(_int_labels.get(int(conf0.get("interval_minutes", 60) or 60), "ogni ora"))

    ttk.Label(tab_cfg, text="Indirizzo del sito HAL").grid(column=0, row=0, sticky="w", pady=4)
    ttk.Entry(tab_cfg, textvariable=v_server, width=42).grid(column=1, row=0, sticky="we", pady=4)

    ttk.Label(tab_cfg, text="Token di accoppiamento").grid(column=0, row=1, sticky="w", pady=4)
    e_token = ttk.Entry(tab_cfg, textvariable=v_token, width=42, show="*")
    e_token.grid(column=1, row=1, sticky="we", pady=4)

    def toggle_token():
        e_token.config(show="" if v_show_token.get() else "*")
    ttk.Checkbutton(tab_cfg, text="mostra", variable=v_show_token, command=toggle_token)\
        .grid(column=2, row=1, sticky="w", padx=(6, 0))

    ttk.Label(tab_cfg, text="Frequenza raccolta").grid(column=0, row=2, sticky="w", pady=4)
    ttk.Combobox(tab_cfg, textvariable=v_interval, state="readonly", width=39,
                 values=[lab for _m, lab in INTERVAL_CHOICES]).grid(column=1, row=2, sticky="we", pady=4)

    ttk.Label(tab_cfg, text="Ignora contenuti più vecchi di (giorni)").grid(column=0, row=3, sticky="w", pady=4)
    ttk.Spinbox(tab_cfg, from_=0, to=365, textvariable=v_days, width=8)\
        .grid(column=1, row=3, sticky="w", pady=4)
    ttk.Label(tab_cfg, text="0 = nessun limite", style="Muted.TLabel")\
        .grid(column=2, row=3, sticky="w", padx=(6, 0))

    lbl_cfg_state = ttk.Label(tab_cfg, text="", wraplength=520, justify="left", style="Muted.TLabel")
    lbl_cfg_state.grid(column=0, row=4, columnspan=3, sticky="w", pady=(12, 8))

    ttk.Label(tab_cfg, style="Muted.TLabel", wraplength=520, justify="left",
              text="Il token si crea sul sito HAL (Config → Agente desktop) e collega questo "
                   "computer al tuo account: da lì l'agente scarica gli Scout da seguire e "
                   "lì rimanda le notizie trovate. Gli Scout si creano sul sito, non qui.")\
        .grid(column=0, row=5, columnspan=3, sticky="w", pady=(0, 12))

    def save_cfg():
        c = cfg.load_config()
        c["server_url"] = v_server.get().strip().rstrip("/")
        c["token"] = v_token.get().strip()
        for mins, lab in INTERVAL_CHOICES:
            if lab == v_interval.get():
                c["interval_minutes"] = mins
        try:
            c["days_limit"] = max(0, int(v_days.get() or 0))
        except ValueError:
            c["days_limit"] = 0
        cfg.save_config(c)
        messagebox.showinfo("HAL Agent", "Impostazioni salvate.\n\nLa nuova frequenza vale "
                                         "dal giro successivo a quello in corso.")

    def test_server():
        c = cfg.load_config()
        c = dict(c, server_url=v_server.get().strip().rstrip("/"), token=v_token.get().strip())
        btn_srv.config(state="disabled", text="Provo…")

        def work():
            ok, msg, _n = remote.probe(c)

            def done():
                btn_srv.config(state="normal", text="Prova il collegamento")
                lbl_cfg_state.config(text=("✔ " if ok else "✘ ") + msg)
                if ok:
                    fill_scouts()
            try:
                root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    btns = ttk.Frame(tab_cfg)
    btns.grid(column=0, row=6, columnspan=3, sticky="w")
    btn_srv = ttk.Button(btns, text="Prova il collegamento", command=test_server)
    btn_srv.grid(column=0, row=0)
    ttk.Button(btns, text="Salva", command=save_cfg).grid(column=1, row=0, padx=8)
    ttk.Button(btns, text="Apri il file JSON (avanzato)", command=_open_json_fallback)\
        .grid(column=2, row=0, padx=8)
    tab_cfg.columnconfigure(1, weight=1)

    # ── aggiornamento periodico ─────────────────────────────────────────────
    def refresh():
        data = telemetry.read()
        st = data.get("status") or {}
        lbl_status.config(text="Stato: %s%s" % (
            st.get("text") or "in attesa",
            ("  ·  %s" % _fmt_ts(st.get("ts"))) if st.get("ts") else ""))
        lbl_next.config(text="Prossima raccolta: " + _fmt_countdown(data.get("next_run_ts")))
        fill_scouts()
        fill_runs()
        refresh_bridge_state()
        root.after(REFRESH_MS, refresh)

    refresh()

    root.lift()
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))

    def _nudge():
        # I Tk vecchi (8.5, quello di sistema di macOS) disegnano il contenuto
        # solo dopo un ridimensionamento: senza questa spinta di un pixel la
        # finestra resta grigia e vuota. Sui Tk moderni non si nota.
        try:
            w, h = root.winfo_width(), root.winfo_height()
            root.geometry("%dx%d" % (w + 1, h))
            root.after(150, lambda: root.geometry("%dx%d" % (w, h)))
        except Exception:
            pass
    root.after(250, _nudge)

    root.mainloop()


def open_panel_process():
    """Apre il pannello in un processo separato (chiamato dalla menu-bar)."""
    try:
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable, "panel"])
        else:
            subprocess.Popen([sys.executable, "-m", "hal_agent", "panel"])
        return True
    except Exception as e:
        log.error("apertura pannello fallita: %s", e)
        return False
