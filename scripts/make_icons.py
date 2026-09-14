#!/usr/bin/env python3
"""Genera le icone di HAL dal marchio disegnato in hal_agent/branding.py.

    python3 scripts/make_icons.py                      # icona dell'app (.icns) + PNG
    python3 scripts/make_icons.py --site ../saas-php/httpdocs/assets

Fa tre cose:
  - assets/HAL.icns        icona del bundle macOS (serve a HAL_Agent.spec)
  - assets/icon-*.png      comodi per Windows/Linux e per il README
  - --site <cartella>      icon-180/192/512.png della PWA del sito
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hal_agent.branding import hal_mark          # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "assets")
# le misure che macOS si aspetta dentro un .iconset
ICONSET = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]


def make_app_icon() -> str:
    os.makedirs(ASSETS, exist_ok=True)
    for s in (16, 32, 64, 128, 256, 512, 1024):
        hal_mark(s).save(os.path.join(ASSETS, f"icon-{s}.png"), "PNG")

    if sys.platform != "darwin":
        return ""
    iconset = os.path.join(ASSETS, "HAL.iconset")
    os.makedirs(iconset, exist_ok=True)
    for base, scale in ICONSET:
        name = f"icon_{base}x{base}.png" if scale == 1 else f"icon_{base}x{base}@2x.png"
        hal_mark(base * scale).save(os.path.join(iconset, name), "PNG")
    icns = os.path.join(ASSETS, "HAL.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    return icns


def make_site_icons(dest: str) -> list:
    os.makedirs(dest, exist_ok=True)
    out = []
    for s in (180, 192, 512):
        p = os.path.join(dest, f"icon-{s}.png")
        hal_mark(s).save(p, "PNG")
        out.append(p)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", help="cartella assets del sito (PWA)")
    a = ap.parse_args()
    icns = make_app_icon()
    print("icona app:", icns or "(solo PNG: non siamo su macOS)")
    if a.site:
        for p in make_site_icons(a.site):
            print("icona sito:", p)
