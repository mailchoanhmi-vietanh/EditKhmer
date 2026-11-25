"""In-place zero-CMD self-updater for VocalForge.exe via GitHub Releases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from backend.config import (
    APP_DATA_DIR,
    APP_VERSION,
    IS_FROZEN,
    IS_WINDOWS,
    hidden_subprocess_kwargs,
)

# Set your GitHub repository "owner/repo" here (or place update_source.json next to EditKhmer.exe)
DEFAULT_GITHUB_REPO = "mailchoanhmi-vietanh/EditKhmer"


def _parse_ver(v: str) -> tuple[int, ...]:
    cleaned = v.strip().lstrip("vV")
    nums: list[int] = []
    for part in cleaned.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums) if nums else (0,)


def get_update_repo() -> str:
    """Resolve GitHub repo slug from update_source.json next to exe or DEFAULT_GITHUB_REPO."""
    exe_dir = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent.parent
    for candidate in (exe_dir / "update_source.json", APP_DATA_DIR / "update_source.json"):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                repo = str(data.get("github_repo") or "").strip()
                if repo:
                    return repo
            except Exception:
                pass
    return os.environ.get("VOCALFORGE_UPDATE_REPO", DEFAULT_GITHUB_REPO).strip()


@dataclass
class UpdateState:
    checking: bool = False
    updating: bool = False
    available: bool = False
    current_version: str = APP_VERSION
    latest_version: str | None = None
    notes: str = ""
    download_url: str | None = None
    asset_size: int = 0
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    configured_repo: str = ""


class AutoUpdater:
    def __init__(self) -> None:
        self.state = UpdateState(configured_repo=get_update_repo())
        self._lock = threading.Lock()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self.state.configured_repo = get_update_repo()
            return asdict(self.state)

    def check_update(self) -> dict[str, Any]:
        repo = get_update_repo()
        with self._lock:
            self.state.configured_repo = repo
            self.state.error = None
            if not repo:
                self.state.available = False
                self.state.message = "Chưa cấu hình kho GitHub cập nhật."
                return asdict(self.state)
            self.state.checking = True

        try:
            url = f"https://api.github.com/repos/{repo}/releases/latest"
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url, headers={"Accept": "application/vnd.github+json"})
                resp.raise_for_status()
                rel = resp.json()

            tag = str(rel.get("tag_name") or "").strip()
            assets = rel.get("assets") or []
            exe_asset = None
            for a in assets:
                if str(a.get("name", "")).lower().endswith(".exe"):
                    exe_asset = a
                    break

            is_newer = bool(tag and _parse_ver(tag) > _parse_ver(APP_VERSION) and exe_asset)
            with self._lock:
                self.state.latest_version = tag.lstrip("vV") if tag else None
                self.state.notes = str(rel.get("body") or "Bản cập nhật mới cải thiện hiệu năng và sửa lỗi.")
                self.state.download_url = exe_asset.get("browser_download_url") if exe_asset else None
                self.state.asset_size = int(exe_asset.get("size") or 0) if exe_asset else 0
                self.state.available = is_newer
                self.state.message = (
                    f"Đã có phiên bản mới v{self.state.latest_version}!"
                    if is_newer
                    else f"Bạn đang dùng bản mới nhất (v{APP_VERSION})."
                )
        except Exception as exc:
            with self._lock:
                self.state.available = False
                self.state.error = str(exc)
                self.state.message = f"Không kiểm tra được cập nhật: {exc}"
        finally:
            with self._lock:
                self.state.checking = False

        return self.get_status()

    def start_update(self) -> dict[str, Any]:
        with self._lock:
            if self.state.updating:
                return asdict(self.state)
            if not self.state.download_url:
                raise RuntimeError("Không có link tải bản cập nhật.")
            if not (IS_WINDOWS and IS_FROZEN):
                raise RuntimeError(
                    "Tự ghi đè .exe chỉ chạy trên bản đóng gói VocalForge.exe ở Windows."
                )
            self.state.updating = True
            self.state.progress = 1.0
            self.state.error = None
            self.state.message = "Đang tải bản cập nhật..."
            dl_url = self.state.download_url

        t = threading.Thread(target=self._download_and_replace, args=(dl_url,), daemon=True)
        t.start()
        return self.get_status()

    def _download_and_replace(self, download_url: str) -> None:
        current_exe = Path(sys.executable).resolve()
        new_exe = current_exe.with_name("EditKhmer_new.exe")
        updater_bat = APP_DATA_DIR / "apply_update.bat"

        try:
            with httpx.Client(follow_redirects=True, timeout=120.0) as client:
                with client.stream("GET", download_url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or self.state.asset_size or 0)
                    done = 0
                    with open(new_exe, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                            f.write(chunk)
                            done += len(chunk)
                            if total > 0:
                                pct = min(98.0, (done / total) * 98.0)
                                self.state.progress = round(pct, 1)
                                self.state.message = (
                                    f"Đang tải bản mới: {done / (1024*1024):.1f} / {total / (1024*1024):.1f} MB"
                                )

            self.state.progress = 99.0
            self.state.message = "Đang áp dụng bản cập nhật và khởi động lại..."

            # Silent batch script: wait for current exe to exit, overwrite it, relaunch
            bat_content = (
                "@echo off\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                ":retry\r\n"
                f'move /y "{new_exe}" "{current_exe}" >nul 2>&1\r\n'
                "if errorlevel 1 (\r\n"
                "  timeout /t 1 /nobreak >nul\r\n"
                "  goto retry\r\n"
                ")\r\n"
                f'start "" "{current_exe}"\r\n'
                'del "%~f0"\r\n'
            )
            updater_bat.write_text(bat_content, encoding="utf-8")

            subprocess.Popen(
                ["cmd.exe", "/c", str(updater_bat)],
                close_fds=True,
                **hidden_subprocess_kwargs(),
            )
            self.state.progress = 100.0
            # Exit current app cleanly so Windows unlocks VocalForge.exe immediately
            os._exit(0)
        except Exception as exc:
            new_exe.unlink(missing_ok=True)
            with self._lock:
                self.state.updating = False
                self.state.error = str(exc)
                self.state.message = f"Lỗi cập nhật: {exc}"


updater = AutoUpdater()
