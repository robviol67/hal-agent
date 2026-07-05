"""
Finestra di configurazione del Ponte LLM (Tkinter).
Gira in un PROPRIO processo (comando `configui`) per non entrare in conflitto
con il run-loop della menu-bar. Scrive su ~/.hal-agent/config.json; l'agente
rilegge la config a ogni giro, quindi le modifiche si applicano da sole.
"""
import logging
import os
import subprocess
import sys
import webbrowser

from . import config as cfg

log = logging.getLogger("hal_agent.configui")

# Preset pronti: locale, tunnel/custom e provider remoti OpenAI-compatibili.
PRESETS = {
    "LM Studio (locale)":      {"provider": "lmstudio", "endpoint": "http://localhost:1234",   "model": "local-model", "needs_key": False},
    "Ollama (locale)":         {"provider": "ollama",   "endpoint": "http://localhost:11434",  "model": "llama3",      "needs_key": False},
    "DeepSeek (remoto)":       {"provider": "deepseek", "endpoint": "https://api.deepseek.com", "model": "deepseek-chat","needs_key": True},
    "Tunnel / Personalizzato": {"provider": "custom",   "endpoint": "",                        "model": "",            "needs_key": False},
}


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
    ttk.Entry(frm, textvariable=v_model, width=34).grid(column=1, row=3, sticky="we", pady=3)

    ttk.Label(frm, text="API key").grid(column=0, row=4, sticky="w")
    ttk.Entry(frm, textvariable=v_key, width=34, show="*").grid(column=1, row=4, sticky="we", pady=3)

    hint = ttk.Label(frm, foreground="#888", wraplength=320, justify="left",
                     text="Funziona con endpoint OpenAI-compatibili: LLM locale, "
                          "LLM locale esposto via tunnel (URL remoto), o provider "
                          "remoti come DeepSeek (richiedono API key e modello).")
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

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=6, columnspan=2, sticky="e")
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
