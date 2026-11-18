"""Percentuale di avanzamento degli invii (v0.9.1).

ProgressFile avvolge un file aperto: chi lo legge (httpx, mentre manda il
multipart) fa avanzare il conto, e la funzione `report(pct)` viene chiamata
al più una volta per punto percentuale e non più spesso di 4 volte al secondo.
`reporter(prefisso, prog)` costruisce quella funzione per la riga «Stato».
"""
import os
import time


class ProgressFile:
    def __init__(self, fh, total: int, report):
        self._fh = fh
        self._total = max(1, int(total))
        self._report = report
        self._done = 0
        self._last_pct = -1
        self._last_t = 0.0

    def read(self, n=-1):
        b = self._fh.read(n)
        if b:
            self._done += len(b)
            self._tick()
        return b

    def _tick(self, force=False):
        pct = min(100, int(self._done * 100 / self._total))
        now = time.monotonic()
        if force or (pct != self._last_pct and now - self._last_t >= 0.25) or pct == 100 and self._last_pct != 100:
            self._last_pct, self._last_t = pct, now
            try:
                self._report(pct)
            except Exception:
                pass

    # httpx misura la lunghezza del file dal descrittore: si delega tutto il resto
    def fileno(self):
        return self._fh.fileno()

    def seek(self, *a):
        return self._fh.seek(*a)

    def tell(self):
        return self._fh.tell()

    def __iter__(self):
        while True:
            b = self.read(64 * 1024)
            if not b:
                return
            yield b


def reporter(prefix: str, prog, size: int = 0):
    """Una funzione pct → «<prefisso> — 42% di 18,3 MB» verso prog (la riga Stato)."""
    tail = f" di {fmt_size(size)}" if size else ""

    def report(pct):
        if prog:
            prog(f"{prefix} — {pct}%{tail}")
    return report


def fmt_size(n: int) -> str:
    if n < 1_000_000:
        return f"{max(1, round(n / 1000))} KB"
    return f"{n / 1_000_000:.1f} MB".replace(".", ",")


def size_of(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
