"""VocalForge Desktop Application Entrypoint (PyWebView + Local FastAPI, Zero-CMD)."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from backend.config import APP_NAME
from backend.server import app as fastapi_app

AUDIO_FILE_TYPES = (
    "Audio & Video (*.mp3;*.wav;*.flac;*.m4a;*.ogg;*.aac;*.wma;*.opus;*.mp4;*.mkv;*.mov;*.webm)",
    "All files (*.*)",
)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_backend_server(port: int) -> threading.Thread:
    config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    # Signal handlers can only be registered on the main thread
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait briefly for local HTTP server to accept connections
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return thread


class DesktopBridge:
    """JavaScript <-> Python native Windows file dialogs (zero-copy for 9-hour files)."""

    def __init__(self) -> None:
        self._window: Any = None

    def bind_window(self, window: Any) -> None:
        self._window = window

    def pick_audio_files(self, allow_multiple: bool = True) -> list[str]:
        if not self._window:
            return []
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=allow_multiple,
            file_types=AUDIO_FILE_TYPES,
        )
        if not result:
            return []
        return [str(Path(p)) for p in result]

    def pick_save_file(self, default_name: str = "EditKhmer_Mix.mp3", fmt: str = "mp3") -> str | None:
        if not self._window:
            return None
        import webview

        ft_map = {
            "mp3": ("MP3 Audio (*.mp3)",),
            "wav": ("WAV Audio (*.wav)",),
            "flac": ("FLAC Audio (*.flac)",),
            "mp4": ("MP4 Video (*.mp4)",),
        }
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=default_name,
            file_types=ft_map.get(fmt.lower(), ("All files (*.*)",)),
        )
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)

    def pick_folder(self) -> str | None:
        if not self._window:
            return None
        import webview

        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        if isinstance(result, (list, tuple)):
            return str(result[0]) if result else None
        return str(result)

    def pick_text_files(self) -> list[str]:
        if not self._window:
            return []
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Phụ đề & Văn bản (*.srt;*.txt)", "All files (*.*)"),
        )
        if not result:
            return []
        return [str(Path(p)) for p in result]

    def pick_image_files(self) -> list[str]:
        if not self._window:
            return []
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Hình ảnh & Logo (*.png;*.jpg;*.jpeg;*.webp;*.gif)", "All files (*.*)"),
        )
        if not result:
            return []
        return [str(Path(p)) for p in result]


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--web", action="store_true", help="Run as local web server without PyWebView window")
    parser.add_argument("--port", type=int, default=0, help="Port to bind (0 = auto)")
    args = parser.parse_args()

    port = args.port or find_free_port()
    url = f"http://127.0.0.1:{port}"
    start_backend_server(port)

    if args.web:
        print(f"{APP_NAME} running at {url}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            sys.exit(0)

    try:
        import webview
    except ImportError:
        print(f"pywebview chưa được cài, chạy chế độ web tại {url}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            sys.exit(0)

    bridge = DesktopBridge()
    window = webview.create_window(
        title=f"{APP_NAME} — Tách Giọng Demucs & Edit Audio",
        url=url,
        js_api=bridge,
        width=1280,
        height=820,
        min_size=(960, 640),
        background_color="#0f1117",
    )
    bridge.bind_window(window)

    # On Windows, Edge WebView2 is built-in on Win10/11 and renders 60fps Canvas smoothly
    webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug=False)


if __name__ == "__main__":
    main()
