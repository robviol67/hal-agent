# PyInstaller spec — costruisce "HAL Agent" (tray app, senza console).
# Uso:  pyinstaller HAL_Agent.spec   (su Mac produce .app, su Windows produce .exe)
# -*- mode: python ; coding: utf-8 -*-
import sys
import re
from pathlib import Path

block_cipher = None

# Versione letta da hal_agent/__init__.py: prima era hardcoded a '0.1.0' e
# mentiva su TUTTE le build (anche la v0.2.6), rendendo impossibile capire
# quale versione fosse installata guardando l'Info.plist.
def _agent_version() -> str:
    src = Path(SPECPATH) / 'hal_agent' / '__init__.py'
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', src.read_text(encoding='utf-8'))
    return m.group(1) if m else '0.0.0'

AGENT_VERSION = _agent_version()

a = Analysis(
    ['run_agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'pystray._darwin' if sys.platform == 'darwin' else 'pystray._win32',
        'tkinter', 'tkinter.ttk', 'tkinter.messagebox',   # pannello + finestra ponte LLM
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='HAL Agent',
    debug=False,
    strip=False,
    upx=True,
    console=False,          # tray app: nessuna finestra console
    disable_windowed_traceback=False,
)

# Su macOS crea anche il bundle .app
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='HAL Agent.app',
        icon=None,
        bundle_identifier='it.hal.agent',
        info_plist={
            'LSUIElement': True,   # app "accessory": solo menu-bar, niente Dock
            'CFBundleShortVersionString': AGENT_VERSION,
            'CFBundleVersion': AGENT_VERSION,
        },
    )
