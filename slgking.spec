# -*- mode: python ; coding: utf-8 -*-

import os

# The exe name is read from the environment so one spec serves both the stable
# build (slgking.exe) and the personal test build (slgking_test.exe). build_exe.bat
# sets SLGKING_EXE_NAME for the test pass; the default is the shipped name.
_EXE_NAME = os.environ.get("SLGKING_EXE_NAME", "slgking")


a = Analysis(
    ['slg_main.py'],
    pathex=[],
    binaries=[],
    # The window sets its own icon at runtime, so the .ico has to be inside the
    # bundle - slg_gui.asset_path reads it from sys._MEIPASS.
    datas=[('assets/slgking.ico', 'assets'),
           ('assets/tag_zh.json', 'assets'),
           ('assets/avatar.png', 'assets'),
           ('assets/slot', 'assets/slot'),
           ('assets/seed/slgking.db', 'assets/seed'),
           ('assets/seed/covers', 'assets/seed/covers')],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowless on purpose: a console build makes Windows allocate a black
    # window before any Python runs, and hiding it still leaves the conhost
    # process alive for the whole session. slg_main._attach_console gives the
    # CLI flags their output back when launched from a real terminal.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # The explorer-facing icon: the exe file itself, and the shortcut that
    # make_shortcut.py points at it.
    icon='assets/slgking.ico',
)
