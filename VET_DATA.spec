# -*- mode: python ; coding: utf-8 -*-
"""Reproducible Windows one-directory build for the supported GUI launcher."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


hiddenimports = (
    collect_submodules("can.io")
    + collect_submodules("canmatrix.formats")
    + [
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "cantools.database.can",
        "canmatrix.convert",
        "openpyxl.cell._writer",
    ]
)
datas = collect_data_files("pyqtgraph") + collect_data_files("canmatrix")

a = Analysis(
    ["vet_data/VET_DATA_merged.py"],
    pathex=["vet_data"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    name="VET_DATA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name="VET_DATA",
)
