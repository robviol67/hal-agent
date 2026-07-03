# PyInstaller spec — costruisce "HAL Agent" (tray app, senza console).
# Uso:  pyinstaller HAL_Agent.spec   (su Mac produce .app, su Windows produce .exe)
# -*- mode: python ; coding: utf-8 -*-
import sys

block_cipher = None

a = Analysis(
    ['run_agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pystray._darwin' if sys.platform == 'darwin' else 'pystray._win32'],
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
            'CFBundleShortVersionString': '0.1.0',
        },
    )
