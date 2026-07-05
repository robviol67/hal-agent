"""
Finestra di configurazione del Ponte LLM (Tkinter).
Gira in un PROPRIO processo (comando `configui`) per non entrare in conflitto
con il run-loop della menu-bar. Scrive su ~/.hal-agent/config.json; l'agente
rilegge la config a ogni giro, quindi le modifiche si applicano da sole.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

from . import config as cfg

log = logging.getLogger("hal_agent.configui")

# Preset pronti: LLM locale o locale esposto via tunnel (URL remoto).
# I provider cloud (Anthropic/Gemini/DeepSeek) si configurano sul SaaS, non qui.
PRESETS = {
    "LM Studio (locale)":      {"provider": "lmstudio", "endpoint": "http://localhost:1234",  "model": "local-model"},
    "Ollama (locale)":         {"provider": "ollama",   "endpoint": "http://localhost:11434", "model": "llama3"},
    "Tunnel / Personalizzato": {"provider": "custom",   "endpoint": "",                       "model": ""},
}


def fetch_models(endpoint: str, api_key: str = ""):
    """
    Elenca i modelli disponibili sull'endpoint OpenAI-compatibile (GET /v1/models).
    Ritorna (lista_id, errore). Usa urllib (stdlib) per non dipendere da altro.
    """
    endpoint = (endpoint or "").rstrip("/")
    if not endpoint:
        return [], "Inserisci prima l'endpoint."
    url = endpoint + ("/models" if endpoint.endswith("/v1") else "/v1/models")
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
        return ids, ("" if ids else "Nessun modello riportato dall'endpoint.")
    except Exception as e:
        return [], str(e)[:140]


def test_generation(endpoint: str, model: str, api_key: str = ""):
    """
    Prova reale di generazione (piccola chat) sull'endpoint OpenAI-compatibile.
    Ritorna (ok: bool, testo_o_errore: str).
    """
    endpoint = (endpoint or "").rstrip("/")
    if not endpoint:
        return False, "Inserisci prima l'endpoint."
    url = endpoint + ("/chat/completions" if endpoint.endswith("/v1") else "/v1/chat/completions")
    body = {
        "model": model or "local-model",
        "messages": [{"role": "user", "content": "Rispondi con una sola parola: ok"}],
        "max_tokens": 16,
        "stream": False,
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                headers={"Content-Type": "application/json"})
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8"))
        choices = d.get("choices") or [{}]
        txt = ((choices[0].get("message") or {}).get("content") or "").strip()
        return True, (txt or "(risposta vuota)")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            detail = str(e)
        return False, "HTTP %s — %s" % (e.code, detail)
    except Exception as e:
        return False, str(e)[:200]


def _open_json_fallback():
    cfg.load_config()
    path = str(cfg.CONFIG_PATH)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        webbrowser.open("file://" + path)


def open_config_window():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception as e:
        log.error("Tkinter non disponibile (%s): apro il file di configurazione", e)
        _open_json_fallback()
        return

    conf = cfg.load_config()
    br = dict(conf.get("llm_bridge", {}) or {})

    root = tk.Tk()
    root.title("HAL Agent — Ponte LLM")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid(sticky="nsew")

    v_enabled  = tk.BooleanVar(value=bool(br.get("enabled")))
    v_preset   = tk.StringVar()
    v_endpoint = tk.StringVar(value=br.get("endpoint", ""))
    v_model    = tk.StringVar(value=br.get("model", ""))
    v_key      = tk.StringVar(value=br.get("api_key", ""))

    ttk.Checkbutton(frm, text="Ponte LLM attivo", variable=v_enabled)\
        .grid(column=0, row=0, columnspan=2, sticky="w", pady=(0, 10))

    ttk.Label(frm, text="Preset").grid(column=0, row=1, sticky="w")
    combo = ttk.Combobox(frm, textvariable=v_preset, values=list(PRESETS.keys()),
                         state="readonly", width=30)
    combo.grid(column=1, row=1, sticky="we", pady=3)

    ttk.Label(frm, text="Endpoint URL").grid(column=0, row=2, sticky="w")
    ttk.Entry(frm, textvariable=v_endpoint, width=34).grid(column=1, row=2, sticky="we", pady=3)

    ttk.Label(frm, text="Modello").grid(column=0, row=3, sticky="w")
    model_row = ttk.Frame(frm)
    model_row.grid(column=1, row=3, sticky="we", pady=3)
    model_row.columnconfigure(0, weight=1)
    model_combo = ttk.Combobox(model_row, textvariable=v_model, width=22)
    model_combo.grid(column=0, row=0, sticky="we")

    def detect_models():
        ids, err = fetch_models(v_endpoint.get().strip(), v_key.get().strip())
        if ids:
            model_combo["values"] = ids
            if not v_model.get() or v_model.get() not in ids:
                v_model.set(ids[0])
            messagebox.showinfo("HAL Agent",
                                "Trovati %d modelli. Scegli quello caricato dall'elenco." % len(ids))
        else:
            messagebox.showwarning("HAL Agent",
                                   "Nessun modello rilevato.\n\n" + (err or "")
                                   + "\n\nControlla che il server LLM sia avviato e l'endpoint corretto.")

    ttk.Button(model_row, text="Rileva modelli", command=detect_models)\
        .grid(column=1, row=0, padx=(6, 0))

    ttk.Label(frm, text="API key").grid(column=0, row=4, sticky="w")
    ttk.Entry(frm, textvariable=v_key, width=34, show="*").grid(column=1, row=4, sticky="we", pady=3)

    hint = ttk.Label(frm, foreground="#888", wraplength=340, justify="left",
                     text="Per LLM locali (LM Studio/Ollama) o un LLM locale esposto via "
                          "tunnel (URL remoto + eventuale API key). Usa «Rileva modelli» "
                          "per scegliere quello caricato ora. I provider cloud "
                          "(Anthropic/Gemini/DeepSeek) si impostano sul sito, in Config → Chiavi AI.")
    hint.grid(column=0, row=5, columnspan=2, sticky="w", pady=(8, 12))

    def on_preset(*_):
        p = PRESETS.get(v_preset.get())
        if not p:
            return
        v_endpoint.set(p["endpoint"])
        v_model.set(p["model"])
    combo.bind("<<ComboboxSelected>>", on_preset)

    def save():
        c = cfg.load_config()
        b = c.setdefault("llm_bridge", {})
        b["enabled"]  = bool(v_enabled.get())
        b["endpoint"] = v_endpoint.get().strip()
        b["model"]    = v_model.get().strip()
        b["api_key"]  = v_key.get().strip()
        p = PRESETS.get(v_preset.get())
        if p:
            b["provider"] = p["provider"]
        b.setdefault("poll_path", "/api/agent/jobs")
        b.setdefault("result_path", "/api/agent/jobs/result")
        cfg.save_config(c)
        messagebox.showinfo("HAL Agent", "Configurazione del ponte salvata.")
        root.destroy()

    # Prova connessione (in un thread, per non bloccare la finestra)
    def test_connection():
        ep, md, key = v_endpoint.get().strip(), v_model.get().strip(), v_key.get().strip()
        btn_test.config(state="disabled", text="Provo…")

        def work():
            ok, msg = test_generation(ep, md, key)

            def done():
                try:
                    btn_test.config(state="normal", text="Prova connessione")
                except Exception:
                    pass
                if ok:
                    messagebox.showinfo("HAL Agent", "Connessione riuscita.\n\nRisposta del modello:\n" + msg)
                else:
                    messagebox.showerror("HAL Agent", "Test fallito:\n\n" + msg
                                         + "\n\nControlla endpoint, modello (usa «Rileva modelli») e che il server LLM sia avviato.")
            try:
                root.after(0, done)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    test_row = ttk.Frame(frm)
    test_row.grid(column=0, row=6, columnspan=2, sticky="w", pady=(0, 8))
    btn_test = ttk.Button(test_row, text="Prova connessione", command=test_connection)
    btn_test.grid(column=0, row=0)

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=7, columnspan=2, sticky="e")
    ttk.Button(btns, text="Apri il file JSON", command=_open_json_fallback)\
        .grid(column=0, row=0, padx=(0, 12))
    ttk.Button(btns, text="Annulla", command=root.destroy).grid(column=1, row=0, padx=4)
    ttk.Button(btns, text="Salva", command=save).grid(column=2, row=0)

    root.update_idletasks()
    try:
        root.eval('tk::PlaceWindow . center')
    except Exception:
        pass
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()
