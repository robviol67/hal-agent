# HAL Agent (desktop) — prototipo

Raccoglitore desktop per **HAL-SaaS**. Gira su **Mac e Windows**, scrappa feed
RSS/Substack/Reddit/YouTube (**con trascrizioni**) usando l'**IP residenziale** del PC
dell'utente e invia le novità al SaaS via API. Opzionalmente fa da **ponte verso un LLM
locale** (Ollama / LM Studio) senza bisogno di alcun tunnel in ingresso.

## Perché
- ✅ Trascrizioni YouTube **gratis** (niente API a pagamento)
- ✅ Nessun blocco da IP datacenter (usa l'IP di casa)
- ✅ Sblocca l'LLM locale (Ollama/LM Studio) via **polling in uscita**
- ✅ Riusa la logica di scraping già collaudata in HAL

## Sviluppo / prova rapida (senza compilare)
```bash
cd desktop-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# un giro singolo che STAMPA il payload (non invia)
python -m hal_agent run --once --dry-run -v

# dove si trova la configurazione
python -m hal_agent config
```
La config sta in `~/.hal-agent/config.json` (creata al primo avvio). Vedi `config.example.json`.

## Modalità
```bash
python -m hal_agent run              # loop periodico (ogni interval_minutes)
python -m hal_agent run --once       # un solo giro e invia
python -m hal_agent run --once --dry-run   # un giro, stampa senza inviare
python -m hal_agent tray             # interfaccia barra di sistema (default se doppio-click)
python -m hal_agent bridge           # ponte LLM locale (Ollama/LM Studio)
```

## Compilare l'eseguibile
- **Mac** (su un Mac): `./build_mac.sh` → `dist/HAL Agent.app` (+ zip)
- **Windows** (su un PC Windows): `powershell -File build_windows.ps1` → `dist\HAL Agent.exe`
- **Entrambi in automatico**: GitHub Actions (`.github/workflows/build.yml`).
  Push di un tag `vX.Y.Z` → crea una Release con **.app (zip) + .exe** allegati.
  Oppure Actions → *build* → *Run workflow* per generarli a mano.

> PyInstaller **non** fa cross-compilazione: il `.exe` si crea su Windows.
> La CI risolve il problema costruendo su runner `windows-latest` + `macos-latest`.

## Contratto API lato SaaS (da implementare nel HAL-PHP)
Invio item raccolti:
```
POST {server_url}/api/agent/ingest
Authorization: Bearer <token>
{ "items": [ {title, excerpt, url, source, published, channel, author, agent}, ... ],
  "agent_version": "0.1.0" }
```
Ponte LLM locale (opzionale):
```
GET  {server_url}/api/agent/jobs          -> { "job": {id, prompt, model, max_tokens} } | { "job": null }
POST {server_url}/api/agent/jobs/result   <- { "job_id": ..., "text": "..." }
```

## Note firma (per evitare avvisi di sicurezza)
- **Mac**: notarizzazione con Apple Developer ID (~$99/anno). Senza firma: tasto destro → *Apri*.
- **Windows**: certificato code-signing (~$100–400/anno). Senza firma: *Ulteriori info* → *Esegui comunque*.
