"""Dati finti dell'agente: scrive hal-agent-data.js (per il design) e i file di stato in HOME/.hal-agent (per le schermate)."""
import json, os, sys, time, hashlib
OUT_JS, HOME = sys.argv[1], sys.argv[2]
now = time.time()
D = lambda s: now - s
FILES = [
 ("Clienti/ErgoSedute/Preventivi", "2026-10-02 Preventivo ErgoSedute sala riunioni.pdf", "pdf", 412_000, 2, 2, "Allineato"),
 ("Clienti/ErgoSedute/Preventivi", "2026-10-28 Preventivo ErgoSedute open space.pdf", "pdf", 388_000, 1, 1, "Allineato"),
 ("Clienti/ErgoSedute/Contratti", "2026-03-11 Contratto ErgoSedute fornitura 2026.docx", "documento", 96_000, 3, 2, "Il Mac è indietro"),
 ("Clienti/ErgoSedute/Contratti", "Condizioni generali ErgoSedute.pdf", "pdf", 1_240_000, 1, 1, "Allineato"),
 ("Clienti/ErgoSedute", "Listino ErgoSedute 2027.xlsx", "foglio", 186_000, 1, 1, "Da decidere"),
 ("Clienti/ErgoSedute/Foto showroom", "showroom-01.jpg", "immagine", 3_400_000, 1, None, "Solo online"),
 ("Clienti/ErgoSedute/Foto showroom", "showroom-02.jpg", "immagine", 3_120_000, 1, None, "Solo online"),
 ("Clienti/ErgoSedute/Foto showroom", "showroom-03.jpg", "immagine", 2_980_000, 1, None, "Solo online"),
 ("Clienti/Studio Bianchi", "2026-09-15 Fattura Studio Bianchi 112.pdf", "pdf", 88_000, 1, 1, "Allineato"),
 ("Clienti/Studio Bianchi", "2026-10-15 Fattura Studio Bianchi 131.pdf", "pdf", 91_000, 1, 1, "Allineato"),
 ("Clienti/Studio Bianchi", "Brief campagna autunno.docx", "documento", 54_000, 1, 1, "Allineato"),
 ("Campagne/NuovoSostenibile", "Palinsesto ottobre.xlsx", "foglio", 64_000, 4, 4, "Allineato"),
 ("Campagne/NuovoSostenibile", "Copertine articoli.key", "presentazione", 18_400_000, 1, 1, "Allineato"),
 ("Campagne/NuovoSostenibile", "Report risultati Q3.pdf", "pdf", 2_100_000, 1, 1, "Da decidere"),
 ("Manoscritti", "L'arte di raccontare — bozza 6.docx", "documento", 1_820_000, 6, 6, "Allineato"),
 ("Manoscritti", "Il Carrier vol. II.docx", "documento", 2_430_000, 2, 2, "Allineato"),
 ("Manoscritti", "Lettera all'editore.rtf", "documento", 12_000, 1, 1, "Allineato"),
 ("In arrivo", "2026-11-18 Scansione.pdf", "scansione", 670_000, 2, None, "Solo online"),
 ("In arrivo", "2026-11-19 Ricevuta parcheggio.pdf", "scansione", 210_000, 1, None, "In coda per il Mac"),
 ("In arrivo", "Certificato stato di famiglia.pdf", "pdf", 98_000, 1, 1, "Allineato"),
 ("Amministrazione", "Bolletta luce ottobre.pdf", "pdf", 240_000, 1, 1, "Allineato"),
 ("Amministrazione", "F24 novembre.pdf", "pdf", 130_000, 1, 1, "Errore"),
 ("Amministrazione/Assicurazioni", "Polizza studio 2026.pdf", "pdf", 760_000, 1, 1, "Allineato"),
 ("", "Note veloci.md", "testo", 4_000, 9, 9, "Allineato"),
 ("", "Logo HAL.svg", "immagine", 22_000, 1, 1, "Allineato"),
]
files, reg, decisions = [], {}, []
for i, (d, n, k, sz, v, mv, st) in enumerate(FILES, start=101):
    req, rs = None, None
    if st == "In coda per il Mac": req, rs = "porta", "in_coda"
    if st == "Errore": req, rs = "porta", "errore"
    files.append({"id": i, "dir": d, "name": n, "kind": k, "size": sz, "version": v, "mac_version": mv,
                  "local_state": "allineato" if mv else "solo_online", "request": req, "request_state": rs,
                  "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(D(3600 * (i - 100))))})
    if mv:
        reg[str(i)] = {"path": (d + "/" if d else "") + n, "version": mv if st != "Il Mac è indietro" else v - 1,
                       "sha": hashlib.sha256(n.encode()).hexdigest(), "size": sz, "mtime": D(86400)}
    if st == "Da decidere":
        decisions.append({"id": len(decisions) + 1, "file_id": i, "kind": "mac_modificato", "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(D(5400)))})
by_kind = {}
for f in files: by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + f["size"]
storage = {"used_bytes": sum(f["size"] for f in files), "files": len(files), "versions_bytes": 9_800_000, "trash_bytes": 1_450_000,
           "by_kind": [{"kind": k, "bytes": b} for k, b in sorted(by_kind.items(), key=lambda x: -x[1])]}
local = {"folder_bytes": sum(f["size"] for f in files if f["mac_version"]), "folder_files": sum(1 for f in files if f["mac_version"]),
         "trash_bytes": 3_200_000, "trash_files": 4, "disk_free": 186_000_000_000, "disk_total": 994_000_000_000}
state = {"running": False, "step": "", "started": D(70), "last_run": D(42), "last_ok": True, "fails": 0, "duration": 3.4,
         "last_result": {"sent": 2, "downloaded": 1, "moved": 1, "deleted": 0, "changed": 1, "errors": 0, "ocr": 1},
         "error": "", "folder": "/Users/marta/Giano", "local": local, "online": storage, "overview_at": D(42),
         "check": {"at": D(86400 * 2), "n": 21, "bad": 0}, "notified_dec": 2}
A = [("invio", "Salito «2026-10-28 Preventivo ErgoSedute open space.pdf» → Clienti/ErgoSedute/Preventivi", True, 300),
     ("invio", "Salito «Brief campagna autunno.docx» → Clienti/Studio Bianchi", True, 320),
     ("scarico", "Portato sul Mac «Certificato stato di famiglia.pdf» → In arrivo", True, 900),
     ("sposto", "Spostato «Polizza studio 2026.pdf» → Amministrazione/Assicurazioni", True, 1500),
     ("modifica", "«Listino ErgoSedute 2027.xlsx» è cambiato sul Mac: aspetta una decisione", True, 5400),
     ("ocr", "Letto il testo di «2026-11-18 Scansione.pdf» (2 pagine): ora è ricercabile", True, 7200),
     ("errore", "«F24 novembre.pdf»: allineamento non riuscito (il server non risponde)", False, 9000),
     ("comando", "«Video showroom.mov» non sale a Giano: più grande di 500 MB", True, 11000),
     ("decisione", "Scelto «Duplica» per «Palinsesto ottobre.xlsx»", True, 86400),
     ("cestino", "«Vecchio listino 2025.pdf» eliminato in Giano: messo nel cestino locale", True, 90000),
     ("comando", "Controllo finito: 21 file verificati, tutti uguali a Giano", True, 172800)]
activity = [{"t": D(s), "k": k, "x": x, "ok": ok} for k, x, ok, s in A]
settings = {"enabled": True, "paused": False, "folder": "/Users/marta/Giano", "interval_seconds": 60, "exclude_ext": ["dmg", "iso"],
            "exclude_dirs": ["node_modules"], "max_mb": 500, "notify": "importanti", "autostart": True}
scouts = [{"name": "Sostenibilità e imprese", "sources": [["RSS", "https://www.lifegate.it/feed"], ["RSS", "https://www.greenme.it/feed/"], ["Reddit", "r/sustainability"]],
           "last_raw": 42, "last_new": 6, "last_sent": 6, "total_sent": 318, "last_sent_ts": D(3600), "last_error": ""},
          {"name": "Ergonomia ufficio", "sources": [["YouTube", "@ergonomiaoggi"], ["RSS", "https://www.officedesign.it/feed"]],
           "last_raw": 18, "last_new": 0, "last_sent": 0, "total_sent": 97, "last_sent_ts": D(86400 * 3), "last_error": ""},
          {"name": "Editoria e scrittura", "sources": [["RSS", "https://www.illibraio.it/feed/"], ["Reddit", "r/writing"], ["RSS", "https://feed-morto.example/rss"]],
           "last_raw": 25, "last_new": 3, "last_sent": 3, "total_sent": 141, "last_sent_ts": D(3600), "last_error": "1 fonte non risponde (feed-morto.example)"}]
runs = [{"ts": D(3600 * h), "raw": 85 - h, "new": max(0, 9 - h), "sent": max(0, 9 - h), "ok": h != 5, "error": "timeout del sito" if h == 5 else ""} for h in range(1, 9)]
bridge = {"enabled": False, "preset": "LM Studio", "endpoint": "http://localhost:1234/v1", "model": "qwen2.5-7b-instruct", "last_check": {"ok": True, "detail": "risponde in 0,8 s", "ts": D(86400)}, "jobs_done": 12}
link = {"server_url": "https://hal-ai.it", "token_set": True, "interval_minutes": 60, "max_age_days": 30, "connected": True, "account": "Marta Riva"}
data = {"agent": {"version": "0.9.6", "update": {"latest": "v0.9.7", "url": "https://github.com/robviol67/hal-agent/releases/latest"},
                  "status": "Giano: 2 inviati, 1 scaricato, 1 da decidere", "next_collect_ts": D(-1800)},
        "giano": {"state": state, "files": files, "decisions": decisions, "registry": reg, "activity": activity, "settings": settings},
        "scouts": scouts, "runs": runs, "bridge": bridge, "link": link,
        "paste": {"projects": ["NuovoSostenibile", "ErgoSedute — lancio sedute 2027", "L'arte di raccontare"]}}
open(OUT_JS, "w", encoding="utf-8").write("/* HAL Agent — dati finti per il design del pannello (scenario: Marta Riva, ErgoSedute).\n"
    "   Forma autorevole: BRIEF_PANNELLO_AGENTE.md §2. Unico punto di scambio dei dati del prototipo. */\n"
    "window.HAL_AGENT_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n")
# file di stato per le schermate del pannello di oggi
H = os.path.join(HOME, ".hal-agent"); os.makedirs(H, exist_ok=True)
W = lambda n, o: open(os.path.join(H, n), "w", encoding="utf-8").write(json.dumps(o, ensure_ascii=False))
W("config.json", {"server_url": "https://hal-ai.it", "giano": settings, "interval_minutes": 60})
W("giano_web.json", {"at": D(42), "files": files, "decisions": decisions})
W("giano_registro.json", {"files": reg, "skipped": {}})
W("giano_stato.json", state)
open(os.path.join(H, "giano_attivita.jsonl"), "w", encoding="utf-8").write("\n".join(json.dumps(a, ensure_ascii=False) for a in reversed(activity)) + "\n")
W("remote_config.json", {"agents": [{"name": s["name"], "rss_feeds": [u for t, u in s["sources"] if t == "RSS"],
    "reddit_subreddits": [u for t, u in s["sources"] if t == "Reddit"], "youtube_channels": [u for t, u in s["sources"] if t == "YouTube"]} for s in scouts]})
W("runtime.json", {"status": {"text": "in attesa", "ts": D(60)}, "runs": [{"ts": r["ts"], "raw": r["raw"], "new": r["new"], "sent": r["sent"], "ok": r["ok"], "error": r["error"],
    "scouts": [{"name": s["name"], "raw": 10, "new": 2, "sent": 2} for s in scouts]} for r in reversed(runs)],
    "scouts": {s["name"]: {"last_raw": s["last_raw"], "last_new": s["last_new"], "last_sent": s["last_sent"], "total_sent": s["total_sent"], "last_sent_ts": s["last_sent_ts"], "last_error": s["last_error"]} for s in scouts},
    "bridge": {}, "next_run_ts": D(-1800)})
W("giano_pannello.json", {"view": "elenco", "open": ["Clienti", "Clienti/ErgoSedute"]})
W("aggiornamento.json", {"latest": "v0.9.7", "checked": D(600)})
print("ok", len(files), "file")
