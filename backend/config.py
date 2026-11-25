"""Configuration, paths, and hidden-subprocess helpers for VocalForge."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

APP_NAME = "EditKhmer"
APP_VERSION = "1.0.0"

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = getattr(sys, "frozen", False)


def get_resource_dir() -> Path:
    """Return directory containing bundled static assets (frontend)."""
    if IS_FROZEN and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_app_data_dir() -> Path:
    """Return writable data directory for runtime, binaries, models, and projects.

    Portable-first: stores everything inside `EditKhmerData/` right next to the
    executable (or project root) so moving or deleting the app folder removes all
    downloaded data cleanly. Falls back to %LOCALAPPDATA%/EditKhmer only if the
    executable directory is not writable (e.g. Program Files).
    """
    exe_dir = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent.parent
    portable_base = exe_dir / "EditKhmerData"
    try:
        portable_base.mkdir(parents=True, exist_ok=True)
        probe = portable_base / ".write_test"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return portable_base
    except OSError:
        if IS_WINDOWS:
            local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            base = Path(local_app_data) / APP_NAME
        else:
            base = Path.home() / ".local" / "share" / APP_NAME
        base.mkdir(parents=True, exist_ok=True)
        return base


APP_DATA_DIR = get_app_data_dir()
BIN_DIR = APP_DATA_DIR / "bin"
RUNTIME_DIR = APP_DATA_DIR / "runtime"
VENV_DIR = RUNTIME_DIR / "venv"
MODELS_DIR = APP_DATA_DIR / "models"
PROJECTS_DIR = APP_DATA_DIR / "projects"
CACHE_DIR = APP_DATA_DIR / "cache"
UPLOADS_DIR = APP_DATA_DIR / "uploads"

for _d in (BIN_DIR, RUNTIME_DIR, MODELS_DIR, PROJECTS_DIR, CACHE_DIR, UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs that completely suppress CMD windows on Windows."""
    kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


def get_ffmpeg_path() -> Path | None:
    """Locate ffmpeg binary (local managed bin first, then system PATH)."""
    exe_name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    local_bin = BIN_DIR / exe_name
    if local_bin.is_file():
        return local_bin
    sys_bin = shutil.which("ffmpeg")
    return Path(sys_bin) if sys_bin else None


def get_ffprobe_path() -> Path | None:
    """Locate ffprobe binary (local managed bin first, then system PATH)."""
    exe_name = "ffprobe.exe" if IS_WINDOWS else "ffprobe"
    local_bin = BIN_DIR / exe_name
    if local_bin.is_file():
        return local_bin
    sys_bin = shutil.which("ffprobe")
    return Path(sys_bin) if sys_bin else None


def get_uv_path() -> Path | None:
    """Locate uv binary (local managed bin first, then system PATH)."""
    exe_name = "uv.exe" if IS_WINDOWS else "uv"
    local_bin = BIN_DIR / exe_name
    if local_bin.is_file():
        return local_bin
    sys_bin = shutil.which("uv")
    return Path(sys_bin) if sys_bin else None


def get_ai_python_path() -> Path | None:
    """Locate Python executable inside the managed AI virtual environment."""
    if IS_WINDOWS:
        candidate = VENV_DIR / "Scripts" / "python.exe"
    else:
        candidate = VENV_DIR / "bin" / "python"
    if candidate.is_file():
        return candidate
    return None


def get_ai_env_vars() -> dict[str, str]:
    """Environment variables for AI subprocesses (cache models inside APP_DATA_DIR)."""
    env = os.environ.copy()
    env["TORCH_HOME"] = str(MODELS_DIR / "torch")
    env["HF_HOME"] = str(MODELS_DIR / "hf")
    env["XDG_CACHE_HOME"] = str(MODELS_DIR / "cache")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Prepend managed bin dir so ffmpeg is always visible to demucs/torchaudio
    path_sep = ";" if IS_WINDOWS else ":"
    env["PATH"] = f"{BIN_DIR}{path_sep}{env.get('PATH', '')}"
    return env


def detect_nvidia_gpu() -> dict[str, Any]:
    """Detect NVIDIA GPU quietly without flashing any console window."""
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi and IS_WINDOWS:
        default_smi = Path(r"C:\Windows\System32\nvidia-smi.exe")
        if default_smi.is_file():
            nvidia_smi = str(default_smi)

    if not nvidia_smi:
        return {"has_gpu": False, "name": None, "vram_mb": 0}

    try:
        proc = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            **hidden_subprocess_kwargs(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            first_line = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            name = parts[0] if parts else "NVIDIA GPU"
            vram = int(float(parts[1])) if len(parts) > 1 and parts[1].isdigit() else 0
            return {"has_gpu": True, "name": name, "vram_mb": vram}
    except Exception:
        pass

    return {"has_gpu": False, "name": None, "vram_mb": 0}
