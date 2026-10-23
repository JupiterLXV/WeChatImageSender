# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project_root = Path(SPEC).resolve().parent
winrt_datas, winrt_binaries, winrt_hidden = collect_all('winrt', include_py_files=False, on_error='raise')
uia_datas, uia_binaries, uia_hidden = collect_all('uiautomation', include_py_files=False, on_error='raise')

a = Analysis(
    [str(project_root / 'main.py')],
    pathex=[str(project_root)],
    binaries=winrt_binaries + uia_binaries,
    datas=[(str(project_root / 'README.md'), '.'), (str(project_root / 'assets'), 'assets')] + winrt_datas + uia_datas,
    hiddenimports=winrt_hidden + uia_hidden,
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
    name='WeChatImageSender',
    icon=str(project_root / 'assets' / 'app-icon.ico'),
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
    name='WeChatImageSender',
)
