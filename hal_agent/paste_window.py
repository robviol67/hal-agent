"""
«Incolla video» (Tkinter): una casella dove incollare link YouTube e mandarli
al Feed di HAL subito, senza scrivere file nella cartella dei video.

Stessa strada della cartella (videos.send_pasted): titolo via oEmbed,
sottotitoli dall'IP di casa, ingest con «solo testo» e progetto; i video senza
sottotitoli li passa a Gemini il giro delle trascrizioni della menu-bar.

Si usa in due posti: come finestra a sé (comando `paste`, voce «Incolla un
video…» del menu) e come scheda del pannello. Il lavoro gira in un thread e
parla con Tk solo attraverso una coda letta con `after` (Tk non è thread-safe).
"""
import logging
import queue
import threading

from . import config as cfg
from . import uikit
from . import videos

log = logging.getLogger("hal_agent.paste")

HINT = ("Incolla uno o più link YouTube, uno per riga. Vanno bene anche una "
        "playlist intera (playlist?list=…) e l'ID nudo del video.")


def _looks_youtube(s: str) -> bool:
    s = (s or "").lower()
    return "youtu" in s or "list=pl" in s


def build_paste_frame(parent, root, standalone: bool = False):
    """Costruisce la casella dentro `parent`. Ritorna (frame, is_busy)."""
    import tkinter as tk
    from tkinter import ttk

    prefs = videos.paste_prefs()
    frm = ttk.Frame(parent, padding=12 if standalone else 10)

    ttk.Label(frm, text=HINT, style="Muted.TLabel", wraplength=520, justify="left")\
        .grid(column=0, row=0, columnspan=3, sticky="w")

    txt = tk.Text(frm, height=6, wrap="word", undo=True)
    txt.grid(column=0, row=1, columnspan=3, sticky="nsew", pady=(6, 4))

    def paste_clipboard():
        try:
            clip = root.clipboard_get()
        except Exception:
            clip = ""
        if not clip.strip():
            return
        cur = txt.get("1.0", "end").strip()
        txt.insert("end", ("\n" if cur else "") + clip.strip() + "\n")
        txt.see("end")

    bar = ttk.Frame(frm)
    bar.grid(column=0, row=2, columnspan=3, sticky="w")
    ttk.Button(bar, text="Incolla dagli appunti", command=paste_clipboard).pack(side="left")
    ttk.Button(bar, text="Svuota", command=lambda: txt.delete("1.0", "end")).pack(side="left", padx=6)

    # ── che cosa ne faccio ──────────────────────────────────────────────────
    ttk.Label(frm, text="Che cosa ne faccio").grid(column=0, row=3, sticky="w", pady=(12, 2))
    v_text = tk.BooleanVar(value=prefs["text_only"])
    ttk.Radiobutton(frm, text="Video da guardare — nel Feed, con la trascrizione allegata",
                    variable=v_text, value=False).grid(column=0, row=4, columnspan=3, sticky="w")
    ttk.Radiobutton(frm, text="Solo il testo — nel Feed come testo (niente → Ascolta)",
                    variable=v_text, value=True).grid(column=0, row=5, columnspan=3, sticky="w")

    ttk.Label(frm, text="Progetto (facoltativo)").grid(column=0, row=6, sticky="w", pady=(10, 0))
    v_proj = tk.StringVar(value="")
    cb = ttk.Combobox(frm, textvariable=v_proj, values=prefs["projects"], width=34)
    cb.grid(column=1, row=6, sticky="w", pady=(10, 0), padx=(8, 0))
    ttk.Label(frm, text="se non esiste, HAL lo crea", style="Muted.TLabel")\
        .grid(column=2, row=6, sticky="w", pady=(10, 0), padx=(8, 0))

    v_resend = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm, text="Rimanda anche i video che HAL ha già ricevuto", variable=v_resend)\
        .grid(column=0, row=7, columnspan=3, sticky="w", pady=(8, 0))

    # ── invio ───────────────────────────────────────────────────────────────
    act = ttk.Frame(frm)
    act.grid(column=0, row=8, columnspan=3, sticky="ew", pady=(12, 4))
    lbl = ttk.Label(act, text="", style="Muted.TLabel")
    lbl.pack(side="left")
    btn = ttk.Button(act, text="Manda a HAL")
    btn.pack(side="right")

    log_box = tk.Text(frm, height=6, wrap="word", relief="flat", background=root.cget("background"))
    log_box.grid(column=0, row=9, columnspan=3, sticky="nsew")
    log_box.configure(state="disabled")

    frm.columnconfigure(2, weight=1)
    frm.rowconfigure(1, weight=1)
    frm.rowconfigure(9, weight=1)

    q = queue.Queue()
    busy = {"on": False}

    def say(line):
        log_box.configure(state="normal")
        log_box.insert("end", line + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    def finish(res):
        busy["on"] = False
        btn.state(["!disabled"])
        if not res.get("ok"):
            lbl.config(text="Non è andata.")
            say("✗ " + (res.get("error_msg") or "errore"))
            if not res.get("sent"):
                return
        parts = []
        if res.get("sent"):
            parts.append(f"{res['sent']} mandati al Feed")
        if res.get("done"):
            parts.append(f"{res['done']} già trascritti")
        if res.get("none"):
            parts.append(f"{res['none']} a Gemini")
        if res.get("error"):
            parts.append(f"{res['error']} da ritentare")
        if res.get("skipped"):
            parts.append(f"{res['skipped']} già ricevuti")
        lbl.config(text=" · ".join(parts) or "Fatto.")
        if res.get("sent"):
            say("✓ Fatto: " + ", ".join(parts) + ". Li trovi nel Feed, Scout «%s»."
                % videos.PASTE_AGENT_NAME)
            txt.delete("1.0", "end")
        if res.get("left"):
            say(f"Ne restano {res['left']}: premi di nuovo «Manda a HAL» per i prossimi "
                f"{videos.PASTE_MAX} (quelli già mandati si saltano da soli).")
            txt.insert("1.0", last["text"])
            v_resend.set(False)     # al prossimo giro si saltano quelli appena mandati
        if res.get("playlist_err"):
            say(f"{res['playlist_err']} playlist non lette (privata o indirizzo sbagliato?).")

    last = {"text": ""}

    def pump():
        try:
            while True:
                kind, data = q.get_nowait()
                if kind == "log":
                    say(data)
                    lbl.config(text=data[:80])
                else:
                    finish(data)
        except queue.Empty:
            pass
        try:
            root.after(150, pump)
        except Exception:
            pass

    def send(_e=None):
        if busy["on"]:
            return "break"
        text = txt.get("1.0", "end").strip()
        if not text:
            lbl.config(text="Incolla prima un link.")
            return "break"
        text_only, project, resend = bool(v_text.get()), v_proj.get().strip(), bool(v_resend.get())
        last["text"] = text
        busy["on"] = True
        btn.state(["disabled"])
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.configure(state="disabled")
        videos.remember_paste_choice(text_only, project)
        cb.configure(values=videos.paste_prefs()["projects"])

        def work():
            try:
                res = videos.send_pasted(text, text_only, project, resend=resend,
                                         on_progress=lambda m: q.put(("log", m)))
            except Exception as e:
                log.exception("invio dei link incollati fallito")
                res = {"ok": False, "error_msg": f"{type(e).__name__}: {e}"}
            q.put(("done", res))
        threading.Thread(target=work, daemon=True).start()
        return "break"

    btn.configure(command=send)
    txt.bind("<Command-Return>", send)
    txt.bind("<Control-Return>", send)

    # appena aperta: se negli appunti c'è un link YouTube, è già nella casella
    if standalone:
        try:
            clip = root.clipboard_get()
        except Exception:
            clip = ""
        if _looks_youtube(clip):
            txt.insert("1.0", clip.strip() + "\n")
    txt.focus_set()

    pump()
    return frm, (lambda: busy["on"])


def open_paste_window():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception as e:
        log.error("Tkinter non disponibile (%s)", e)
        return

    cfg.load_config()
    root = tk.Tk()
    root.title("HAL Agent — Incolla video")
    root.geometry("600x560")
    root.minsize(520, 460)
    try:
        style = ttk.Style()
        style.configure("Muted.TLabel", foreground="#777")
    except Exception:
        pass

    frm, is_busy = build_paste_frame(root, root, standalone=True)
    frm.pack(fill="both", expand=True)

    def on_close():
        if is_busy() and not messagebox.askyesno(
                "HAL Agent", "Sto ancora mandando i video: se chiudi adesso, quelli non ancora "
                             "partiti restano qui. Chiudo lo stesso?"):
            return
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    uikit.bring_to_front(root)
    root.mainloop()
