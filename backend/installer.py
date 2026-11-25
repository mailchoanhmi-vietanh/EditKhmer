"""Automatic zero-CMD environment setup & dependency downloader for VocalForge."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import tarfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from backend.config import (
    APP_DATA_DIR,
    BIN_DIR,
    IS_WINDOWS,
    RUNTIME_DIR,
    VENV_DIR,
    detect_nvidia_gpu,
    get_ai_env_vars,
    get_ai_python_path,
    get_ffmpeg_path,
    get_ffprobe_path,
    get_uv_path,
    hidden_subprocess_kwargs,
)

# Official download URLs
FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_WIN_FALLBACK_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
UV_WIN_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
UV_LINUX_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz"


@dataclass
class InstallState:
    running: bool = False
    stage: str = "idle"
    message: str = ""
    progress: float = 0.0
    error: str | None = None
    logs: list[str] = field(default_factory=list)

    def log(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.logs.append(text)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]


class EnvironmentInstaller:
    """Checks and automatically installs FFmpeg, uv, Python 3.11, PyTorch (CUDA/CPU), and Demucs."""

    def __init__(self) -> None:
        self.state = InstallState()
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()

    def check_environment(self) -> dict[str, Any]:
        """Return detailed readiness status of all components."""
        gpu = detect_nvidia_gpu()
        ffmpeg = get_ffmpeg_path()
        ffprobe = get_ffprobe_path()
        uv = get_uv_path()
        ai_py = get_ai_python_path()

        demucs_ok = False
        torch_cuda = False
        torch_version = None

        if ai_py:
            try:
                proc = subprocess.run(
                    [
                        str(ai_py),
                        "-c",
                        (
                            "import torch, demucs; "
                            "print(torch.__version__, '|', torch.cuda.is_available())"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=get_ai_env_vars(),
                    **hidden_subprocess_kwargs(),
                )
                if proc.returncode == 0 and "|" in proc.stdout:
                    ver, cuda_flag = [x.strip() for x in proc.stdout.strip().splitlines()[-1].split("|")]
                    demucs_ok = True
                    torch_version = ver
                    torch_cuda = cuda_flag.lower() == "true"
            except Exception:
                pass

        ready = bool(ffmpeg and ffprobe and ai_py and demucs_ok)
        return {
            "ready": ready,
            "data_dir": str(APP_DATA_DIR),
            "gpu": gpu,
            "components": {
                "ffmpeg": {"installed": bool(ffmpeg and ffprobe), "path": str(ffmpeg) if ffmpeg else None},
                "uv": {"installed": bool(uv), "path": str(uv) if uv else None},
                "ai_runtime": {
                    "installed": bool(ai_py and demucs_ok),
                    "python": str(ai_py) if ai_py else None,
                    "torch_version": torch_version,
                    "cuda_enabled": torch_cuda,
                },
            },
            "installer": {
                "running": self.state.running,
                "stage": self.state.stage,
                "message": self.state.message,
                "progress": round(self.state.progress, 1),
                "error": self.state.error,
                "logs": self.state.logs[-30:],
            },
        }

    def start_install(self, force_cpu: bool = False) -> bool:
        """Start background installation if not already running."""
        with self._lock:
            if self.state.running:
                return False
            self._cancel_event.clear()
            self.state = InstallState(
                running=True,
                stage="starting",
                message="Đang kiểm tra môi trường...",
                progress=1.0,
            )

        thread = threading.Thread(
            target=self._run_install,
            args=(force_cpu,),
            daemon=True,
        )
        thread.start()
        return True

    def cancel_install(self) -> None:
        self._cancel_event.set()

    def _download_file(self, url: str, dest: Path, start_pct: float, end_pct: float, label: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.with_suffix(dest.suffix + ".part")
        self.state.log(f"Tải {label}: {url}")

        with httpx.Client(follow_redirects=True, timeout=60.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                downloaded = 0
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        if self._cancel_event.is_set():
                            raise RuntimeError("Người dùng đã hủy cài đặt.")
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            ratio = min(1.0, downloaded / total)
                            self.state.progress = start_pct + ratio * (end_pct - start_pct)
                            mb_done = downloaded / (1024 * 1024)
                            mb_tot = total / (1024 * 1024)
                            self.state.message = f"Đang tải {label}: {mb_done:.1f} / {mb_tot:.1f} MB"
                        else:
                            mb_done = downloaded / (1024 * 1024)
                            self.state.message = f"Đang tải {label}: {mb_done:.1f} MB"

        shutil.move(str(tmp_path), str(dest))

    def _install_ffmpeg(self) -> None:
        if get_ffmpeg_path() and get_ffprobe_path():
            self.state.log("FFmpeg đã có sẵn, bỏ qua bước tải.")
            self.state.progress = 20.0
            return

        self.state.stage = "ffmpeg"
        if not IS_WINDOWS:
            raise RuntimeError(
                "Trên Linux/macOS vui lòng cài ffmpeg qua package manager (vd: sudo apt install ffmpeg)."
            )

        zip_path = RUNTIME_DIR / "ffmpeg.zip"
        try:
            self._download_file(FFMPEG_WIN_URL, zip_path, 2.0, 16.0, "FFmpeg")
        except Exception as exc:
            self.state.log(f"Mirror chính lỗi ({exc}), thử mirror dự phòng...")
            self._download_file(FFMPEG_WIN_FALLBACK_URL, zip_path, 2.0, 16.0, "FFmpeg (dự phòng)")

        self.state.message = "Đang giải nén FFmpeg & FFprobe..."
        self.state.progress = 17.0
        extracted = 0
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                base = Path(member.filename).name.lower()
                if base in ("ffmpeg.exe", "ffprobe.exe"):
                    target = BIN_DIR / base
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted += 1
                    self.state.log(f"Đã giải nén {base}")

        zip_path.unlink(missing_ok=True)
        if extracted < 2:
            raise RuntimeError("Không tìm thấy ffmpeg.exe và ffprobe.exe trong gói tải về.")
        self.state.progress = 20.0

    def _install_uv(self) -> Path:
        existing = get_uv_path()
        if existing:
            self.state.log(f"Đã có uv tại {existing}")
            self.state.progress = 28.0
            return existing

        self.state.stage = "uv"
        if IS_WINDOWS:
            archive = RUNTIME_DIR / "uv.zip"
            self._download_file(UV_WIN_URL, archive, 20.0, 26.0, "Bộ cài nhanh uv")
            self.state.message = "Đang giải nén uv..."
            with zipfile.ZipFile(archive, "r") as zf:
                for member in zf.infolist():
                    if Path(member.filename).name.lower() == "uv.exe":
                        target = BIN_DIR / "uv.exe"
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        break
            archive.unlink(missing_ok=True)
            uv_path = BIN_DIR / "uv.exe"
        else:
            archive = RUNTIME_DIR / "uv.tar.gz"
            self._download_file(UV_LINUX_URL, archive, 20.0, 26.0, "uv")
            with tarfile.open(archive, "r:gz") as tf:
                for member in tf.getmembers():
                    if Path(member.name).name == "uv":
                        target = BIN_DIR / "uv"
                        f_obj = tf.extractfile(member)
                        if f_obj:
                            with open(target, "wb") as dst:
                                shutil.copyfileobj(f_obj, dst)
                            target.chmod(target.stat().st_mode | stat.S_IEXEC)
                        break
            archive.unlink(missing_ok=True)
            uv_path = BIN_DIR / "uv"

        if not uv_path.is_file():
            raise RuntimeError("Giải nén uv thất bại.")
        self.state.progress = 28.0
        return uv_path

    def _run_hidden_streamed(self, cmd: list[str], env: dict[str, str], step_label: str) -> None:
        self.state.log(f"$ {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **hidden_subprocess_kwargs(),
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if self._cancel_event.is_set():
                proc.terminate()
                raise RuntimeError("Người dùng đã hủy cài đặt.")
            clean = line.strip()
            if clean:
                self.state.log(clean)
                self.state.message = f"{step_label}: {clean[:90]}"

        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"Bước '{step_label}' kết thúc với mã lỗi {code}.")

    def _run_install(self, force_cpu: bool) -> None:
        try:
            self._install_ffmpeg()
            uv_path = self._install_uv()

            env = get_ai_env_vars()
            env["UV_PYTHON_INSTALL_DIR"] = str(RUNTIME_DIR / "python")
            env["UV_CACHE_DIR"] = str(RUNTIME_DIR / "uv_cache")

            # Step 3: Create isolated Python 3.11 venv via uv (auto-downloads Python if missing)
            self.state.stage = "python"
            self.state.progress = 32.0
            self.state.message = "Đang khởi tạo Python 3.11 độc lập cho AI..."
            if not get_ai_python_path():
                self._run_hidden_streamed(
                    [str(uv_path), "venv", "--python", "3.11", str(VENV_DIR)],
                    env,
                    "Tạo môi trường Python 3.11",
                )

            ai_py = get_ai_python_path()
            if not ai_py:
                raise RuntimeError("Không tìm thấy Python sau khi tạo môi trường AI.")

            # Step 4: Install PyTorch (CUDA 12.1 if NVIDIA GPU detected, else CPU)
            gpu = detect_nvidia_gpu()
            use_cuda = gpu["has_gpu"] and not force_cpu
            self.state.stage = "torch"
            self.state.progress = 45.0

            if use_cuda:
                self.state.message = f"Phát hiện {gpu['name']} — Đang tải PyTorch CUDA..."
                torch_index = "https://download.pytorch.org/whl/cu121"
            else:
                self.state.message = "Đang tải PyTorch (chế độ CPU)..."
                torch_index = "https://download.pytorch.org/whl/cpu"

            self._run_hidden_streamed(
                [
                    str(uv_path),
                    "pip",
                    "install",
                    "--python",
                    str(ai_py),
                    "torch==2.4.1",
                    "torchaudio==2.4.1",
                    "--index-url",
                    torch_index,
                ],
                env,
                "Cài đặt PyTorch",
            )

            # Step 5: Install Demucs + soundfile
            self.state.stage = "demucs"
            self.state.progress = 80.0
            self.state.message = "Đang cài đặt Demucs AI & thư viện audio..."
            self._run_hidden_streamed(
                [
                    str(uv_path),
                    "pip",
                    "install",
                    "--python",
                    str(ai_py),
                    "demucs",
                    "soundfile",
                ],
                env,
                "Cài đặt Demucs",
            )

            self.state.stage = "done"
            self.state.progress = 100.0
            self.state.message = "Cài đặt hoàn tất! Ứng dụng đã sẵn sàng sử dụng."
            self.state.log("Hoàn tất thiết lập môi trường VocalForge.")
        except Exception as exc:
            self.state.stage = "error"
            self.state.error = str(exc)
            self.state.message = f"Lỗi cài đặt: {exc}"
            self.state.log(f"ERROR: {exc}")
        finally:
            self.state.running = False


installer = EnvironmentInstaller()
