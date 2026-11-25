# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec to build a single, lightweight, zero-CMD VocalForge.exe."""

from PyInstaller.utils.hooks import collect_submodules

hidden_imports = (
    collect_submodules("uvicorn")
    + collect_submodules("fastapi")
    + collect_submodules("starlette")
    + collect_submodules("webview")
    + collect_submodules("edge_tts")
    + ["httpx", "multipart", "python_multipart", "email_validator", "aiohttp"]
)

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("frontend", "frontend"),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the .exe lightweight (~25-35 MB); AI runtime is auto-downloaded on first run
        "torch",
        "torchaudio",
        "demucs",
        "numpy",
        "scipy",
        "matplotlib",
        "tkinter",
        "pytest",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EditKhmer",
    icon="assets/icon.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Zero CMD window on Windows!
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
