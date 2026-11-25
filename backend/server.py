"""FastAPI local backend for VocalForge."""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.audio_engine import export_manager, generate_waveform_peaks
from backend.config import (
    APP_NAME,
    APP_VERSION,
    IS_WINDOWS,
    PROJECTS_DIR,
    UPLOADS_DIR,
    get_resource_dir,
    hidden_subprocess_kwargs,
)
from backend.demucs_worker import demucs_manager
from backend.installer import installer
from backend.tts_worker import TTS_VOICES, tts_manager
from backend.updater import updater

app = FastAPI(title=APP_NAME, version=APP_VERSION)

FRONTEND_DIR = get_resource_dir() / "frontend"


class InstallRequest(BaseModel):
    force_cpu: bool = False


class CreateJobRequest(BaseModel):
    input_path: str
    model: str = "htdemucs"
    stems: str = "2stems"
    output_format: str = "mp3"
    device: str = "auto"
    chunk_duration: float = Field(default=300.0, ge=30.0, le=1200.0)
    output_dir: str | None = None


class ExportRequest(BaseModel):
    clips: list[dict[str, Any]]
    output_path: str | None = None
    output_dir: str | None = None
    output_format: str = "mp3"
    file_name: str = "VocalForge_Mix"
    export_mode: str = "mix"  # "mix" | "selected_track" | "separate_tracks" | "video_mp4"
    tracks: list[dict[str, Any]] | None = None
    video_source_path: str | None = None
    video_resolution: str = "original"  # "original" | "720" | "1080" | "1440" | "2160"
    video_fps: str = "original"  # "original" | "24" | "30" | "60"
    overlays: list[dict[str, Any]] | None = None


class OpenFolderRequest(BaseModel):
    path: str


# ---------------- Environment / Auto-Setup API ----------------


@app.get("/api/env")
def api_get_env() -> dict[str, Any]:
    return installer.check_environment()


@app.post("/api/env/install")
def api_start_install(req: InstallRequest) -> dict[str, Any]:
    started = installer.start_install(force_cpu=req.force_cpu)
    return {"started": started, "state": installer.check_environment()["installer"]}


@app.post("/api/env/cancel")
def api_cancel_install() -> dict[str, Any]:
    installer.cancel_install()
    return {"cancelled": True}


# ---------------- Demucs Separation Jobs API ----------------


@app.get("/api/jobs")
def api_list_jobs() -> dict[str, Any]:
    return {"jobs": demucs_manager.list_jobs()}


@app.post("/api/jobs")
def api_create_job(req: CreateJobRequest) -> dict[str, Any]:
    try:
        job = demucs_manager.create_job(
            input_path=req.input_path,
            model=req.model,
            stems=req.stems,
            output_format=req.output_format,
            device=req.device,
            chunk_duration=req.chunk_duration,
            output_dir=req.output_dir,
        )
        return {"job": job}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/pause")
def api_pause_job(job_id: str) -> dict[str, Any]:
    return {"ok": demucs_manager.pause_job(job_id)}


@app.post("/api/jobs/{job_id}/resume")
def api_resume_job(job_id: str) -> dict[str, Any]:
    return {"ok": demucs_manager.start_or_resume(job_id)}


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str) -> dict[str, Any]:
    return {"ok": demucs_manager.cancel_job(job_id)}


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str) -> dict[str, Any]:
    return {"ok": demucs_manager.delete_job(job_id)}


# ---------------- Audio Peaks, Streaming & Upload API ----------------


@app.post("/api/audio/upload")
async def api_upload_audio(file: UploadFile = File(...)) -> dict[str, Any]:
    """Fallback upload endpoint for browser drag-and-drop (desktop mode uses direct file paths)."""
    safe_name = Path(file.filename or "audio.mp3").name
    target = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    with open(target, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    return {"path": str(target.resolve()), "name": safe_name}


@app.get("/api/audio/peaks")
def api_audio_peaks(
    path: str = Query(..., description="Absolute path to local audio file"),
    points: int = Query(default=2400, ge=200, le=8000),
) -> dict[str, Any]:
    try:
        return generate_waveform_peaks(path, target_points=points)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/audio/stream")
def api_stream_audio(request: Request, path: str = Query(...)):
    """Stream local audio with HTTP 206 Partial Content so seeking a 9-hour file is instant."""
    file_path = Path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file audio.")

    file_size = file_path.stat().st_size
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "audio/mpeg"

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path=file_path, media_type=mime_type, headers={"Accept-Ranges": "bytes"})

    # Parse "bytes=start-end"
    try:
        unit, range_spec = range_header.strip().split("=", 1)
        if unit.lower() != "bytes":
            raise ValueError("Unsupported range unit")
        start_str, end_str = range_spec.split("-", 1)
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else min(start + 2 * 1024 * 1024 - 1, file_size - 1)
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
    except Exception:
        return FileResponse(path=file_path, media_type=mime_type, headers={"Accept-Ranges": "bytes"})

    chunk_len = end - start + 1

    def iter_range():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = chunk_len
            while remaining > 0:
                data = f.read(min(65536, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_len),
    }
    return StreamingResponse(iter_range(), status_code=206, media_type=mime_type, headers=headers)


# ---------------- Multitrack Export API ----------------


@app.post("/api/export")
def api_start_export(req: ExportRequest) -> dict[str, Any]:
    try:
        ext = req.output_format.lower().strip(".")
        safe_stem = "".join(c for c in req.file_name if c.isalnum() or c in ("-", "_", " ")).strip() or "VocalForge"
        base_dir = Path(req.output_dir) if req.output_dir else (PROJECTS_DIR / "exports")
        base_dir.mkdir(parents=True, exist_ok=True)

        if req.export_mode == "video_mp4":
            if not req.video_source_path:
                raise ValueError("Vui lòng chọn file Video gốc để ghép tiếng.")
            out_path = req.output_path or str(base_dir / f"{safe_stem}.mp4")
            task = export_manager.start_video_export(
                req.clips,
                req.video_source_path,
                out_path,
                resolution=req.video_resolution,
                fps=req.video_fps,
                overlays=req.overlays,
            )
            return {"task": task}

        if req.export_mode == "separate_tracks" and req.tracks:
            out_dir = req.output_path or str(base_dir / f"{safe_stem}_Stems")
            task = export_manager.start_separate_tracks_export(req.tracks, out_dir, ext)
            return {"task": task}

        out_path = req.output_path or str(base_dir / f"{safe_stem}.{ext}")
        task = export_manager.start_export(req.clips, out_path, ext)
        return {"task": task}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/export/{task_id}")
def api_get_export(task_id: str) -> dict[str, Any]:
    task = export_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ xuất file.")
    return {"task": task}


@app.post("/api/system/open-folder")
def api_open_folder(req: OpenFolderRequest) -> dict[str, Any]:
    target = Path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Đường dẫn không tồn tại.")
    if IS_WINDOWS:
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)], **hidden_subprocess_kwargs())
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        opener = shutil.which("xdg-open") or shutil.which("open")
        if opener:
            folder = target.parent if target.is_file() else target
            subprocess.Popen([opener, str(folder)])
    return {"ok": True}


class CreateTTSJobRequest(BaseModel):
    source_name: str = "van_ban.txt"
    file_path: str | None = None
    content: str | None = None
    mode: str = "auto"  # "auto" | "srt" | "txt"
    voice: str = "vi-VN-HoaiMyNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    auto_fit: bool = True
    pause_between_txt: float = 0.35
    output_format: str = "mp3"


# ---------------- Edge-TTS (.srt & .txt) API ----------------


@app.get("/api/tts/voices")
def api_tts_voices() -> dict[str, Any]:
    return {"voices": TTS_VOICES}


@app.get("/api/tts/jobs")
def api_tts_list_jobs() -> dict[str, Any]:
    return {"jobs": tts_manager.list_jobs()}


@app.post("/api/tts/jobs")
def api_tts_create_job(req: CreateTTSJobRequest) -> dict[str, Any]:
    try:
        text = req.content
        name = req.source_name
        if req.file_path:
            p = Path(req.file_path)
            if not p.is_file():
                raise FileNotFoundError(f"Không tìm thấy file: {req.file_path}")
            text = p.read_text(encoding="utf-8", errors="replace")
            name = p.name
        if not text or not text.strip():
            raise ValueError("Nội dung file .srt / .txt đang trống.")

        job = tts_manager.create_job(
            source_name=name,
            content=text,
            mode=req.mode,
            voice=req.voice,
            rate=req.rate,
            pitch=req.pitch,
            auto_fit=req.auto_fit,
            pause_between_txt=req.pause_between_txt,
            output_format=req.output_format,
        )
        return {"job": job}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tts/jobs/{job_id}/pause")
def api_tts_pause(job_id: str) -> dict[str, Any]:
    return {"ok": tts_manager.pause_job(job_id)}


@app.post("/api/tts/jobs/{job_id}/resume")
def api_tts_resume(job_id: str) -> dict[str, Any]:
    return {"ok": tts_manager.start_or_resume(job_id)}


@app.delete("/api/tts/jobs/{job_id}")
def api_tts_delete(job_id: str) -> dict[str, Any]:
    return {"ok": tts_manager.delete_job(job_id)}


# ---------------- Auto-Updater API ----------------


@app.get("/api/update/check")
def api_check_update() -> dict[str, Any]:
    return updater.check_update()


@app.get("/api/update/status")
def api_update_status() -> dict[str, Any]:
    return updater.get_status()


@app.post("/api/update/start")
def api_start_update() -> dict[str, Any]:
    try:
        return updater.start_update()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------- Static Frontend ----------------

if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
