#!/bin/bash
# Costruisce l'app Mac (.app + zip). Da lanciare SU MacOS.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv 2>/dev/null || true
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
