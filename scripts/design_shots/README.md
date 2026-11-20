# Foto del pannello per il design

1. `python3 -I fakedata.py <handover>/hal-agent-data.js <HOME_FINTA>` scrive i dati finti e i file di stato.
2. Dal repo, con lo schermo SBLOCCATO: `HOME=<HOME_FINTA> PYTHONPATH=$PWD .venv/bin/python scripts/design_shots/shots.py scripts/design_shots <cartella_foto>`.
   Fotografa solo la finestra del pannello (`winshot.py`, mai lo schermo intero).
