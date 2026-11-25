"""Chunked, resumable Demucs separation worker engineered for 9+ hour audio files."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.config import (
    PROJECTS_DIR,
    get_ai_env_vars,
    get_ai_python_path,
    get_ffmpeg_path,
    get_ffprobe_path,
    hidden_subprocess_kwargs,
)


@dataclass
class ChunkSpec:
    index: int
    core_start: float
    core_end: float
    extract_start: float
    extract_end: float

    @property
    def extract_duration(self) -> float:
        return max(0.0, self.extract_end - self.extract_start)

    @property
    def trim_offset(self) -> float:
        return max(0.0, self.core_start - self.extract_start)

    @property
    def core_duration(self) -> float:
        return max(0.0, self.core_end - self.core_start)


def compute_chunks(
    total_duration: float,
    chunk_duration: float = 300.0,
    overlap: float = 4.0,
) -> list[ChunkSpec]:
    """Split a long timeline into exact non-overlapping cores with padded extraction margins.

    Each chunk is extracted with `overlap` seconds of audio context on both ends so Demucs
    avoids boundary artifacts, then trimmed strictly back to `[core_start, core_end]` before
    concatenation so a 9-hour file stays sample-accurate with zero drift.
    """
    if total_duration <= 0:
        return []
    chunk_duration = max(15.0, float(chunk_duration))
    overlap = max(0.0, min(float(overlap), chunk_duration / 4.0))

    chunks: list[ChunkSpec] = []
    count = math.ceil(total_duration / chunk_duration)
    for idx in range(count):
        core_start = idx * chunk_duration
        core_end = min(total_duration, (idx + 1) * chunk_duration)
        extract_start = max(0.0, core_start - overlap)
        extract_end = min(total_duration, core_end + overlap)
        chunks.append(
            ChunkSpec(
                index=idx,
                core_start=round(core_start, 4),
                core_end=round(core_end, 4),
                extract_start=round(extract_start, 4),
                extract_end=round(extract_end, 4),
            )
        )
    return chunks


def probe_duration(file_path: str | Path) -> float:
    """Return media duration in seconds using ffprobe (no console window)."""
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        raise RuntimeError("Chưa có ffprobe. Vui lòng chạy bước thiết lập tự động.")
    proc = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        **hidden_subprocess_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Không đọc được thời lượng file: {proc.stderr.strip()}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"Thời lượng file không hợp lệ: {proc.stdout!r}") from exc


@dataclass
class SeparationJob:
    id: str
    input_path: str
    input_name: str
    output_dir: str
    model: str = "htdemucs"
    stems: str = "2stems"  # "2stems" or "4stems"
    output_format: str = "mp3"  # "mp3", "wav", "flac"
    device: str = "auto"  # "auto", "cuda", "cpu"
    chunk_duration: float = 300.0
    overlap: float = 4.0
    total_duration: float = 0.0
    total_chunks: int = 0
    completed_chunks: list[int] = field(default_factory=list)
    status: str = "queued"  # queued, running, paused, merging, completed, failed, cancelled
    progress: float = 0.0
    message: str = "Đang chờ..."
    error: str | None = None
    output_files: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    eta_seconds: float | None = None

    @property
    def stem_names(self) -> list[str]:
        if self.stems == "2stems":
            return ["vocals", "no_vocals"]
        return ["vocals", "drums", "bass", "other"]

    def save(self) -> None:
        self.updated_at = time.time()
        job_dir = PROJECTS_DIR / self.id
        job_dir.mkdir(parents=True, exist_ok=True)
        meta_path = job_dir / "job.json"
        tmp_path = job_dir / "job.json.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        shutil.move(str(tmp_path), str(meta_path))

    @classmethod
    def load(cls, meta_path: Path) -> SeparationJob:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        job = cls(**data)
        # If app closed mid-run, mark as paused so user can resume cleanly
        if job.status in ("running", "merging", "queued"):
            job.status = "paused"
            job.message = f"Đã tạm dừng ở đoạn {len(job.completed_chunks)}/{job.total_chunks} — bấm Tiếp tục để chạy tiếp"
            job.save()
        return job


class DemucsManager:
    """Manages chunked separation jobs with pause/resume persistence."""

    def __init__(self) -> None:
        self._jobs: dict[str, SeparationJob] = {}
        self._lock = threading.Lock()
        self._pause_flags: dict[str, threading.Event] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._active_procs: dict[str, subprocess.Popen[str]] = {}
        self._load_existing_jobs()

    def _load_existing_jobs(self) -> None:
        if not PROJECTS_DIR.exists():
            return
        for job_dir in PROJECTS_DIR.iterdir():
            meta = job_dir / "job.json"
            if meta.is_file():
                try:
                    job = SeparationJob.load(meta)
                    self._jobs[job.id] = job
                except Exception:
                    pass

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [asdict(j) for j in jobs]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return asdict(job) if job else None

    def create_job(
        self,
        input_path: str,
        model: str = "htdemucs",
        stems: str = "2stems",
        output_format: str = "mp3",
        device: str = "auto",
        chunk_duration: float = 300.0,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        src = Path(input_path)
        if not src.is_file():
            raise FileNotFoundError(f"Không tìm thấy file: {input_path}")

        duration = probe_duration(src)
        if duration <= 0:
            raise ValueError("File audio không có dữ liệu hoặc thời lượng bằng 0.")

        chunks = compute_chunks(duration, chunk_duration=chunk_duration, overlap=4.0)
        job_id = uuid.uuid4().hex[:12]
        out_dir = Path(output_dir) if output_dir else (PROJECTS_DIR / job_id / "output")
        out_dir.mkdir(parents=True, exist_ok=True)

        job = SeparationJob(
            id=job_id,
            input_path=str(src.resolve()),
            input_name=src.name,
            output_dir=str(out_dir.resolve()),
            model=model,
            stems=stems,
            output_format=output_format,
            device=device,
            chunk_duration=chunk_duration,
            overlap=4.0,
            total_duration=round(duration, 3),
            total_chunks=len(chunks),
            status="queued",
            message=f"Đã chia thành {len(chunks)} đoạn — sẵn sàng tách",
        )
        job.save()

        with self._lock:
            self._jobs[job.id] = job

        self.start_or_resume(job.id)
        return asdict(job)

    def start_or_resume(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in ("running", "merging", "completed"):
                return False
            pause_ev = threading.Event()
            cancel_ev = threading.Event()
            self._pause_flags[job_id] = pause_ev
            self._cancel_flags[job_id] = cancel_ev
            job.status = "running"
            job.error = None
            job.message = "Đang khởi động Demucs..."
            job.save()

        t = threading.Thread(target=self._worker_loop, args=(job_id,), daemon=True)
        t.start()
        return True

    def pause_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "running":
                return False
            ev = self._pause_flags.get(job_id)
            if ev:
                ev.set()
            proc = self._active_procs.get(job_id)
            if proc and proc.poll() is None:
                proc.terminate()
            job.message = "Đang tạm dừng an toàn..."
            return True

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            ev = self._cancel_flags.get(job_id)
            if ev:
                ev.set()
            proc = self._active_procs.get(job_id)
            if proc and proc.poll() is None:
                proc.terminate()
            if job.status not in ("running", "merging"):
                job.status = "cancelled"
                job.message = "Đã hủy"
                job.save()
            return True

    def delete_job(self, job_id: str) -> bool:
        self.cancel_job(job_id)
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        job_dir = PROJECTS_DIR / job_id
        shutil.rmtree(job_dir, ignore_errors=True)
        return True

    def _run_subprocess(self, job_id: str, cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
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
        with self._lock:
            self._active_procs[job_id] = proc

        output_tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.strip()
            if clean:
                output_tail.append(clean)
                if len(output_tail) > 25:
                    output_tail.pop(0)
        code = proc.wait()

        with self._lock:
            self._active_procs.pop(job_id, None)

        return code, "\n".join(output_tail)

    def _worker_loop(self, job_id: str) -> None:
        job = self._jobs[job_id]
        pause_ev = self._pause_flags[job_id]
        cancel_ev = self._cancel_flags[job_id]

        ffmpeg = get_ffmpeg_path()
        ai_py = get_ai_python_path()
        if not ffmpeg or not ai_py:
            job.status = "failed"
            job.error = "Thiếu FFmpeg hoặc môi trường Demucs AI. Hãy chạy Cài đặt tự động trước."
            job.message = job.error
            job.save()
            return

        work_dir = PROJECTS_DIR / job.id / "work"
        chunks_in_dir = work_dir / "in"
        chunks_raw_dir = work_dir / "raw"
        chunks_out_dir = work_dir / "stems"
        for d in (chunks_in_dir, chunks_raw_dir, chunks_out_dir):
            d.mkdir(parents=True, exist_ok=True)

        chunks = compute_chunks(job.total_duration, job.chunk_duration, job.overlap)
        env = get_ai_env_vars()
        chunk_times: list[float] = []

        try:
            for chunk in chunks:
                if cancel_ev.is_set():
                    job.status = "cancelled"
                    job.message = "Đã hủy tác vụ"
                    job.save()
                    return
                if pause_ev.is_set():
                    job.status = "paused"
                    job.message = f"Đã tạm dừng ở đoạn {len(job.completed_chunks)}/{job.total_chunks}"
                    job.save()
                    return

                if chunk.index in job.completed_chunks:
                    continue

                t0 = time.monotonic()
                done_count = len(job.completed_chunks)
                job.progress = round((done_count / max(1, job.total_chunks)) * 92.0, 1)
                job.message = f"Đang tách đoạn {chunk.index + 1}/{job.total_chunks}..."
                job.save()

                # 1. Extract padded chunk as 44.1kHz stereo WAV
                chunk_wav = chunks_in_dir / f"chunk_{chunk.index:05d}.wav"
                extract_cmd = [
                    str(ffmpeg),
                    "-y",
                    "-ss",
                    f"{chunk.extract_start:.4f}",
                    "-t",
                    f"{chunk.extract_duration:.4f}",
                    "-i",
                    job.input_path,
                    "-vn",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    "-c:a",
                    "pcm_s16le",
                    str(chunk_wav),
                ]
                code, out = self._run_subprocess(job_id, extract_cmd)
                if cancel_ev.is_set() or pause_ev.is_set():
                    continue
                if code != 0:
                    raise RuntimeError(f"FFmpeg cắt đoạn {chunk.index + 1} lỗi:\n{out}")

                # 2. Run Demucs on this chunk only (frees GPU VRAM cleanly after each chunk)
                demucs_cmd = [
                    str(ai_py),
                    "-m",
                    "demucs.separate",
                    "-n",
                    job.model,
                    "-o",
                    str(chunks_raw_dir),
                ]
                if job.stems == "2stems":
                    demucs_cmd.extend(["--two-stems", "vocals"])
                if job.device in ("cuda", "cpu"):
                    demucs_cmd.extend(["-d", job.device])
                demucs_cmd.append(str(chunk_wav))

                code, out = self._run_subprocess(job_id, demucs_cmd, env=env)
                if cancel_ev.is_set() or pause_ev.is_set():
                    continue
                if code != 0:
                    raise RuntimeError(f"Demucs lỗi ở đoạn {chunk.index + 1}:\n{out}")

                # 3. Trim each separated stem strictly back to its exact core window
                stem_src_dir = chunks_raw_dir / job.model / chunk_wav.stem
                for stem in job.stem_names:
                    src_stem = stem_src_dir / f"{stem}.wav"
                    if not src_stem.is_file():
                        raise RuntimeError(f"Không tìm thấy stem '{stem}' sau khi tách đoạn {chunk.index + 1}.")
                    stem_dir = chunks_out_dir / stem
                    stem_dir.mkdir(parents=True, exist_ok=True)
                    trimmed_stem = stem_dir / f"part_{chunk.index:05d}.flac"
                    trim_cmd = [
                        str(ffmpeg),
                        "-y",
                        "-ss",
                        f"{chunk.trim_offset:.4f}",
                        "-t",
                        f"{chunk.core_duration:.4f}",
                        "-i",
                        str(src_stem),
                        "-c:a",
                        "flac",
                        "-compression_level",
                        "1",
                        str(trimmed_stem),
                    ]
                    code, out = self._run_subprocess(job_id, trim_cmd)
                    if code != 0:
                        raise RuntimeError(f"Lỗi cắt biên stem {stem} đoạn {chunk.index + 1}:\n{out}")

                # 4. Clean raw temporary WAVs immediately to keep disk usage tiny even for 9-hour files
                chunk_wav.unlink(missing_ok=True)
                shutil.rmtree(stem_src_dir, ignore_errors=True)

                job.completed_chunks.append(chunk.index)
                elapsed = time.monotonic() - t0
                chunk_times.append(elapsed)
                avg_t = sum(chunk_times[-5:]) / len(chunk_times[-5:])
                remaining = job.total_chunks - len(job.completed_chunks)
                job.eta_seconds = round(avg_t * remaining, 1)
                job.progress = round((len(job.completed_chunks) / max(1, job.total_chunks)) * 92.0, 1)
                job.save()

            # 5. Merge all trimmed core segments per stem into final files
            job.status = "merging"
            job.message = "Đang ghép nối các đoạn thành file hoàn chỉnh..."
            job.progress = 94.0
            job.eta_seconds = None
            job.save()

            base_name = Path(job.input_name).stem
            out_dir = Path(job.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            ext = job.output_format.lower()
            codec_args = {
                "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
                "flac": ["-c:a", "flac"],
                "wav": ["-c:a", "pcm_s16le"],
            }.get(ext, ["-c:a", "libmp3lame", "-b:a", "320k"])

            final_outputs: dict[str, str] = {}
            for s_idx, stem in enumerate(job.stem_names):
                list_file = work_dir / f"concat_{stem}.txt"
                stem_dir = chunks_out_dir / stem
                with open(list_file, "w", encoding="utf-8") as f:
                    for c in chunks:
                        part = (stem_dir / f"part_{c.index:05d}.flac").resolve()
                        escaped = str(part).replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")

                final_path = out_dir / f"{base_name}_{stem}.{ext}"
                merge_cmd = [
                    str(ffmpeg),
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_file),
                    *codec_args,
                    str(final_path),
                ]
                code, out = self._run_subprocess(job_id, merge_cmd)
                if code != 0:
                    raise RuntimeError(f"Lỗi ghép file stem {stem}:\n{out}")

                final_outputs[stem] = str(final_path)
                job.progress = round(94.0 + ((s_idx + 1) / len(job.stem_names)) * 6.0, 1)
                job.save()

            # Cleanup chunk workspace once merged successfully
            shutil.rmtree(work_dir, ignore_errors=True)

            job.output_files = final_outputs
            job.status = "completed"
            job.progress = 100.0
            job.message = "Tách giọng hoàn tất!"
            job.save()

        except Exception as exc:
            if cancel_ev.is_set():
                job.status = "cancelled"
                job.message = "Đã hủy tác vụ"
            elif pause_ev.is_set():
                job.status = "paused"
                job.message = f"Đã tạm dừng ở đoạn {len(job.completed_chunks)}/{job.total_chunks}"
            else:
                job.status = "failed"
                job.error = str(exc)
                job.message = f"Lỗi: {exc}"
            job.save()


demucs_manager = DemucsManager()
