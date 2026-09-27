#!/bin/bash
# Costruisce l'app Mac (.app + zip). Da lanciare SU MacOS.
set -euo pipefail
cd "$(dirname "$0")"
# ⚠️ NIENTE Python di Apple (/usr/bin/python3): porta Tk 8.5.9 del 2011, che sui
# macOS recenti lascia le finestre GRIGIE (pannello, «Incolla video», cartelle).
# Si usa un Python 3.12 autonomo di uv (Tk 9), installato al volo se manca.
PY=""
if command -v uv >/dev/null 2>&1; then
  uv python install 3.12 >/dev/null 2>&1 || true
  PY="$(uv python find 3.12 2>/dev/null || true)"
fi
if [ -z "$PY" ]; then
  echo "❌ Serve uv (brew install uv): il Python di sistema ha un Tk troppo vecchio." >&2
  exit 1
fi
# la venv si rifà se è di un altro Python (es. quella vecchia col Tk 8.5)
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys,tkinter; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)' 2>/dev/null; then
  rm -rf .venv
  "$PY" -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt pyinstaller -q
rm -rf build dist
pyinstaller HAL_Agent.spec --noconfirm
# Zip del .app pronto da distribuire
if [ -d "dist/HAL Agent.app" ]; then
  ( cd dist && zip -r -q "HAL-Agent-mac.zip" "HAL Agent.app" )
  echo "✅ Fatto: dist/HAL Agent.app  +  dist/HAL-Agent-mac.zip"
else
  echo "✅ Fatto: vedi cartella dist/"
fi
