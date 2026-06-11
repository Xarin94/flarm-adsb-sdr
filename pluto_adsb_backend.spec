# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_submodules


def existing_libiio_binaries():
    candidates = []
    seen = set()
    explicit = [
        os.environ.get("PLUTO_ADSB_LIBIIO_DIR"),
        r"C:\Program Files\SDR-Radio.com (V3)",
        r"C:\Program Files\libiio",
        r"C:\Program Files\Analog Devices\libiio",
    ]
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]

    def add_file(path):
        if not path:
            return
        if os.path.isdir(path):
            path = os.path.join(path, "libiio.dll")
        if not os.path.isfile(path):
            return
        normalized = os.path.abspath(path)
        if normalized.lower() in seen:
            return
        seen.add(normalized.lower())
        candidates.append((normalized, "."))

    for value in explicit:
        add_file(value)

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for current_root, _dirs, files in os.walk(root):
            if "libiio.dll" in files:
                add_file(os.path.join(current_root, "libiio.dll"))

    return candidates


a = Analysis(
    ["pluto_adsb_tracker.py"],
    pathex=[],
    binaries=existing_libiio_binaries(),
    datas=[("static", "static")],
    hiddenimports=collect_submodules("adi") + collect_submodules("iio"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pluto-adsb-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="pluto-adsb-backend",
)
