# HAL Agent (desktop) — prototipo

Raccoglitore desktop per **HAL-SaaS**. Gira su **Mac e Windows**, scrappa feed
RSS/Substack/Reddit/YouTube (**con trascrizioni intere**) usando l'**IP residenziale** del PC
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
python -m hal_agent panel            # Pannello: Scout, invii, ponte LLM, collegamento
python -m hal_agent bridge           # ponte LLM locale (Ollama/LM Studio)
python -m hal_agent transcribe       # coda trascrizioni del sito (+ sveglia Gemini)
python -m hal_agent video --once     # elenchi di link YouTube (cartella osservata) → Feed
python -m hal_agent transcript URL   # prova: stampa la trascrizione di un video
```

## Parole chiave (v0.6.1)
Le parole chiave di uno Scout filtrano **solo RSS e Reddit** (su titolo e sommario).
I **canali YouTube si prendono interi**: un canale è già tematico, e titoli e descrizioni
dei video sono spesso fuorvianti.

## Trascrizioni dei video YouTube (v0.6)
I video dei canali YouTube degli Scout arrivano nel Feed **con la trascrizione intera**
(sottotitoli di YouTube, manuali o automatici: gratis, un secondo a video). Si decide
per Scout, dal sito («Trascrivi i video dei canali YouTube», acceso di default).

| Via | Chi lavora | Quando |
|---|---|---|
| **Sottotitoli** | l'agente (IP di casa) | sempre, prima |
| **Gemini** (ascolta il video) | il server, su richiesta dell'agente | solo se i sottotitoli mancano, e solo se l'opzione è accesa in Configurazione |

Oltre agli Scout:
- **dal Feed**: bottone «Trascrivi» su qualsiasi video YouTube raccolto (va in coda, l'agente
  la prende entro un minuto); «Trascrivi ora con Gemini» salta la coda;
- **cartella dei video** (menu-bar → «Video»): una cartella con file `.txt`, **un link YouTube
  per riga** (`#` = commento). Ogni link nuovo viene trascritto e mandato al Feed come
  elemento dello Scout «Cartella video». I file non vengono modificati: si continua ad
  aggiungere righe.

### Video «solo testo» (di alcuni interessa la trascrizione, non il video)

Un link può essere marcato: HAL non lo tratta come qualcosa da guardare, ma appena la
trascrizione è pronta la archivia da sola in **«Leggi»**, come documento, nel progetto
indicato. Tre modi, dal più generale al più preciso (l'ultimo vince):

| Dove | Come | Vale per |
|---|---|---|
| **Cartella** | una sottocartella `Solo testo` (o `Trascrizioni`) | tutti i link lì dentro |
| **File** | una riga `#! testo` | tutto il file, ovunque sia scritta |
| **Riga** | `[testo]` in fondo alla riga | quel link soltanto |

Dopo la freccia si indica il **progetto** di destinazione — e il progetto si può chiedere
anche per un video che resta da guardare:

```
#! testo → Ricerca AI
https://youtu.be/AAAAAAAAAAA               solo testo, progetto «Ricerca AI»
https://youtu.be/BBBBBBBBBBB  [video]      eccezione: questo si guarda
https://youtu.be/CCCCCCCCCCC  [testo → Frontiera]
https://youtu.be/DDDDDDDDDDD  [→ Ricerca AI]   resta un video, ma finisce nel progetto
```

Il progetto viene cercato fra i tuoi (senza badare a maiuscole; «Ricerca» trova «Ricerca AI»
se è l'unico che somiglia) e, se non esiste, **viene creato**. Aggiungere il marcatore a un
link **già mandato** lo rimanda: HAL non duplica la notizia, la rimarca e archivia il testo.
Toglierlo invece non cancella niente di già archiviato.

Serve la migration `/api/migrate_video_text.php` sul sito.

### Playlist

Al posto dei singoli link si può incollare l'indirizzo di una **playlist**: vale per tutti i
video che contiene, marcatore compreso.

```
https://youtube.com/playlist?list=PLfGPUS4FpAm9pbXXUGLPWjSN64CvIHfKv  [testo → Guida Claude]
```

- Niente chiave API: si legge la pagina pubblica della playlist, 100 video per pagina, fino a
  `playlist_max` (200 di serie).
- La playlist resta **sotto osservazione**: viene riletta ogni `playlist_refresh_minutes`
  (60 di serie) — «Leggi gli elenchi ora» la rilegge subito — quindi **i video aggiunti dopo
  arrivano da soli**. I video tolti dalla playlist restano nel Feed: quello che è arrivato non
  si cancella da sé.
- L'invio resta a `max_per_run` video per giro (20 di serie): una playlist lunga entra a
  scaglioni, non tutta insieme.
- Un link `watch?v=…&list=…` resta **un video solo**: per prendere tutta la playlist serve
  l'indirizzo della playlist (quello con `playlist?list=`), o l'id nudo `PL…`.

### Non ti va di scrivere file?

Sul sito c'è **«Aggiungi video»** (`/aggiungi.php`): si incolla e basta — link, playlist o
un canale `@nome` — si sceglie dove mandarli e in quale progetto, senza nessuna sintassi da
ricordare. Funziona anche dal telefono e per chi l'agente non ce l'ha.

Le playlist e i canali sorvegliati dal sito li legge **il server**; l'agente fa solo da
sveglia (`/api/agent/watch`, ogni `transcripts.watch_minutes` = 20 minuti).

Nella cartella dei video l'agente lascia un promemoria — `COME SI SCRIVE.txt` — con questa
stessa sintassi e qualche esempio. Se lo cancelli non torna.

Il loop «Trascrizioni per il sito» gira sempre (una GET al minuto a vuoto) e si può spegnere
dal menu; gli Scout trascrivono comunque i propri canali durante la raccolta.

### Il freno (quando YouTube dice «troppe richieste»)

Ogni tanto YouTube risponde **429 / IpBlocked** al download dei sottotitoli: non è colpa del
video, è il tuo indirizzo che ha chiesto troppo — e insistere allunga il castigo. Al primo
blocco l'agente **si ferma da solo**: mezz'ora, poi un'ora, due, quattro, sei. Durante la
pausa:

- non chiede più sottotitoli a YouTube (risponde subito, senza rete);
- **non prende lavori dalla coda** del sito: restano lì, li farà dopo — così non si bruciano
  i tentativi e non si finisce su Gemini senza volerlo;
- il fallback Gemini continua, perché lo esegue il server col suo indirizzo.

La **prima trascrizione riuscita toglie il freno**. Nel menu compare, solo mentre la pausa è
in corso, la voce «⏸ YouTube in pausa ancora N min — riprova ora»: cliccandola si riparte
subito (se YouTube blocca di nuovo, la pausa riprende più lunga). Lo stato sta in
`~/.hal-agent/state.json`, sotto `transcripts.brake_*`.

## Le finestre (pannello, ponte LLM, scelta cartella)

L'agente è un'app di **menu-bar** (`LSUIElement`): niente icona nel Dock. Le finestre di un
processo così nascono **dietro** a tutte le altre e macOS non le disegna nemmeno — restano
rettangoli grigi. `hal_agent/uikit.py` rimedia: appena la finestra esiste promuove il
processo ad app normale (`NSApplicationActivationPolicyRegular`, via ctypes: nessuna
dipendenza in più), la porta davanti e le dà una spintarella di geometria che costringe Tk
a ridisegnare.

⚠️ **L'ordine conta**: toccare `NSApplication` *prima* di creare la finestra fa terminare il
processo — Tk installa una propria sottoclasse (`TKApplication`) e trovarne una già fatta gli
fa lanciare `-[NSApplication _setup:]: unrecognized selector`. Prima Tk, poi AppKit.

Per controllare come stanno le cose: `python -m hal_agent uidiag` (funziona anche sul
bundle: `"HAL Agent.app/Contents/MacOS/HAL Agent" uidiag`) stampa politica di attivazione
prima e dopo, e dice se la finestra è sopravvissuta.

## Il Pannello (dalla menu-bar: «Apri pannello…», o click sullo stato)
Finestra unica per capire cosa sta facendo l'agente, senza aprire il JSON:

| Scheda | Cosa mostra |
|---|---|
| **Scout collegati** | gli Scout scaricati dal sito, quante fonti hanno, raccolti/nuovi/inviati dell'ultimo giro, totale inviati, ultimo invio. Selezionando uno Scout: parole chiave, elenco fonti e **quali fonti non hanno risposto e perché** |
| **Invii recenti** | ultimi 30 giri con esito (inviati / niente di nuovo / invio fallito) e, per ogni giro, il dettaglio per Scout |
| **Ponte LLM** | a cosa serve, stato reale (acceso/spento, modello raggiungibile, lavori eseguiti) e la sua configurazione |
| **Collegamento** | indirizzo del sito, token, frequenza, limite giorni + «Prova il collegamento» |

Il pannello gira in un processo separato (Tk vuole il suo main-loop) e parla con
l'agente tramite i file in `~/.hal-agent/`:
`runtime.json` (diario: stato, storico giri, contatori per Scout, ponte) scritto dal
runner, `trigger` scritto dal pannello per chiedere una raccolta immediata.

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
Trascrizioni (v0.6): gli item dell'ingest possono portare `transcript` + `transcript_status`
(done | none | error); la coda del sito:
```
GET  {server_url}/api/agent/transcribe?limit=5  -> { ok, jobs:[{id,url,video_id}], fallback_pending }
POST {server_url}/api/agent/transcribe          <- { results:[{id,status,transcript,lang,detail}] }
POST {server_url}/api/agent/transcribe          <- { fallback:1 }   -> { processed, remaining }  (Gemini, un video)
```

## Note firma (per evitare avvisi di sicurezza)
- **Mac**: notarizzazione con Apple Developer ID (~$99/anno). Senza firma: tasto destro → *Apri*.
- **Windows**: certificato code-signing (~$100–400/anno). Senza firma: *Ulteriori info* → *Esegui comunque*.
