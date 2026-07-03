"""Entrypoint per il pacchetto eseguibile (PyInstaller).

Senza argomenti avvia la tray app; con argomenti si comporta come la CLI
(`run --once`, `bridge`, ecc.), utile per debug del binario.
"""
import sys
from hal_agent.__main__ import main

if __name__ == "__main__":
    # Se lanciato senza argomenti (doppio click), avvia la tray.
    if len(sys.argv) == 1:
        sys.argv.append("tray")
    sys.exit(main())
