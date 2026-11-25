"""Resumable Edge-TTS worker supporting timed .srt (with auto-fit) and long .txt files."""

from __future__ import annotations

import asyncio
import json
import re
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
    get_ffmpeg_path,
    hidden_subprocess_kwargs,
)
from backend.demucs_worker import probe_duration

TTS_VOICES = [
    {"id": "vi-VN-HoaiMyNeural", "label": "Tiếng Việt — Hoài My (Nữ)"},
    {"id": "vi-VN-NamMinhNeural", "label": "Tiếng Việt — Nam Minh (Nam)"},
    {"id": "km-KH-SreymomNeural", "label": "ភាសាខ្មែរ (Khmer) — Sreymom (Nữ)"},
    {"id": "km-KH-PisethNeural", "label": "ភាសាខ្មែរ (Khmer) — Piseth (Nam)"},
    {"id": "en-US-JennyNeural", "label": "English — Jenny (Female)"},
    {"id": "en-US-GuyNeural", "label": "English — Guy (Male)"},
    {"id": "zh-CN-XiaoxiaoNeural", "label": "中文 — Xiaoxiao (Female)"},
    {"id": "ja-JP-NanamiNeural", "label": "日本語 — Nanami (Female)"},
    {"id": "ko-KR-SunHiNeural", "label": "한국어 — SunHi (Female)"},
]

_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    ms_val = int(ms.ljust(3, "0")[:3])
    return int(h) * 3600 + int(m) * 60 + int(s) + ms_val / 1000.0


def parse_srt(content: str) -> list[dict[str, Any]]:
    """Parse SRT content into timed cue dicts: {index, start, end, text}."""
    cleaned = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", cleaned.strip())
    cues: list[dict[str, Any]] = []

    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        match = None
        time_line_idx = -1
        for i, ln in enumerate(lines[:2]):
            m = _SRT_TIME_RE.search(ln)
            if m:
                match = m
                time_line_idx = i
                break
        if not match:
            continue
        start = _to_seconds(*match.groups()[:4])
        end = _to_seconds(*match.groups()[4:])
        text_lines = lines[time_line_idx + 1 :]
        text = " ".join(text_lines)
        text = _HTML_TAG_RE.sub("", text).strip()
        if not text:
            continue
        cues.append(
            {
                "index": len(cues),
                "start": round(start, 3),
                "end": round(max(end, start + 0.2), 3),
                "text": text,
            }
        )
    return cues


def parse_txt(content: str, max_chars: int = 380) -> list[dict[str, Any]]:
    """Split plain text into clean, natural-length segments for Edge-TTS."""
    cleaned = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    raw_paragraphs = [p.strip() for p in cleaned.split("\n") if p.strip()]
    segments: list[dict[str, Any]] = []

    for para in raw_paragraphs:
        if len(para) <= max_chars:
            segments.append({"index": len(segments), "start": 0.0, "end": 0.0, "text": para})
            continue
        # Split long paragraph by sentence punctuation (Latin, CJK, and Khmer ។ / ៕)
        sentences = re.split(r"(?<=[.!?;…。！？។៕])\s*", para)
        buf = ""
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(buf) + len(sent) + 1 <= max_chars:
                buf = f"{buf} {sent}".strip()
            else:
                if buf:
                    segments.append({"index": len(segments), "start": 0.0, "end": 0.0, "text": buf})
                # If a single sentence is still longer than max_chars, slice safely
                while len(sent) > max_chars:
                    cut = sent.rfind(" ", 0, max_chars)
                    if cut < 40:
                        cut = max_chars
                    segments.append({"index": len(segments), "start": 0.0, "end": 0.0, "text": sent[:cut].strip()})
                    sent = sent[cut:].strip()
                buf = sent
        if buf:
            segments.append({"index": len(segments), "start": 0.0, "end": 0.0, "text": buf})

    return segments


def build_atempo_chain(speed: float) -> str:
    """Build valid FFmpeg atempo filter chain supporting any speed (including > 2.0)."""
    speed = max(0.5, float(speed))
    parts: list[str] = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


@dataclass
class TTSJob:
    id: str
    source_name: str
    mode: str  # "srt" or "txt"
    voice: str = "vi-VN-HoaiMyNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    auto_fit: bool = True
    max_speed: float = 2.0
    concurrency: int = 6
    pause_between_txt: float = 0.35
    output_format: str = "mp3"
    total_items: int = 0
    completed_items: list[int] = field(default_factory=list)
    status: str = "queued"  # queued, running, paused, merging, completed, failed, cancelled
    progress: float = 0.0
    message: str = "Đang chờ..."
    error: str | None = None
    output_file: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    eta_seconds: float | None = None

    def job_dir(self) -> Path:
        return PROJECTS_DIR / f"tts_{self.id}"

    def save(self) -> None:
        self.updated_at = time.time()
        d = self.job_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "tts_job.json.tmp"
        dst = d / "tts_job.json"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        shutil.move(str(tmp), str(dst))

    @classmethod
    def load(cls, path: Path) -> TTSJob:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        job = cls(**filtered)
        if job.status in ("running", "merging", "queued"):
            job.status = "paused"
            job.message = f"Đã tạm dừng ở câu {len(job.completed_items)}/{job.total_items}"
            job.save()
        return job


class TTSManager:
    """Manages 6-thread concurrent, resumable Edge-TTS jobs for .srt and .txt files."""

    def __init__(self) -> None:
        self._jobs: dict[str, TTSJob] = {}
        self._lock = threading.Lock()
        self._pause_flags: dict[str, threading.Event] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        if not PROJECTS_DIR.exists():
            return
        for d in PROJECTS_DIR.iterdir():
            meta = d / "tts_job.json"
            if meta.is_file():
                try:
                    job = TTSJob.load(meta)
                    self._jobs[job.id] = job
                except Exception:
                    pass

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [asdict(j) for j in jobs]

    def create_job(
        self,
        source_name: str,
        content: str,
        mode: str = "auto",
        voice: str = "vi-VN-HoaiMyNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        auto_fit: bool = True,
        pause_between_txt: float = 0.35,
        output_format: str = "mp3",
    ) -> dict[str, Any]:
        ext = Path(source_name).suffix.lower()
        if mode == "auto":
            detected_mode = "srt" if (ext == ".srt" or _SRT_TIME_RE.search(content)) else "txt"
        else:
            detected_mode = mode

        items = parse_srt(content) if detected_mode == "srt" else parse_txt(content)
        if not items:
            raise ValueError("Không tìm thấy nội dung lời thoại hợp lệ trong file.")

        job_id = uuid.uuid4().hex[:10]
        job = TTSJob(
            id=job_id,
            source_name=source_name,
            mode=detected_mode,
            voice=voice,
            rate=rate,
            pitch=pitch,
            auto_fit=auto_fit,
            concurrency=6,
            pause_between_txt=pause_between_txt,
            output_format=output_format,
            total_items=len(items),
            status="queued",
            message=f"Đã đọc {len(items)} câu ({detected_mode.upper()}) — chạy 6 luồng",
        )
        d = job.job_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "items.json", "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
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
            self._pause_flags[job_id] = threading.Event()
            self._cancel_flags[job_id] = threading.Event()
            job.status = "running"
            job.error = None
            job.message = "Đang chạy Edge-TTS (6 luồng song song)..."
            job.save()

        t = threading.Thread(target=self._worker, args=(job_id,), daemon=True)
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
            job.message = "Đang tạm dừng..."
            return True

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            ev = self._cancel_flags.get(job_id)
            if ev:
                ev.set()
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        shutil.rmtree(job.job_dir(), ignore_errors=True)
        return True

    async def _synthesize_one(
        self,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        out_mp3: Path,
        max_retries: int = 4,
    ) -> bool:
        import random
        import edge_tts

        for attempt in range(1, max_retries + 1):
            try:
                comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
                await comm.save(str(out_mp3))
                if out_mp3.is_file() and out_mp3.stat().st_size > 100:
                    await asyncio.sleep(random.uniform(0.15, 0.4))
                    return True
            except Exception:
                if attempt < max_retries:
                    await asyncio.sleep(attempt * 2.5)
        return False

    def _run_ffmpeg(self, cmd: list[str]) -> None:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[-350:] or "FFmpeg lỗi khi ghép TTS")

    def _make_silence_wav(self, ffmpeg: Path, duration: float, out_wav: Path) -> None:
        self._run_ffmpeg(
            [
                str(ffmpeg),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
                "-t",
                f"{max(0.05, duration):.3f}",
                "-c:a",
                "pcm_s16le",
                str(out_wav),
            ]
        )

    def _worker(self, job_id: str) -> None:
        job = self._jobs[job_id]
        pause_ev = self._pause_flags[job_id]
        cancel_ev = self._cancel_flags[job_id]

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            job.status = "failed"
            job.error = "Chưa có FFmpeg. Vui lòng chạy Tải & Cài tự động trước."
            job.message = job.error
            job.save()
            return

        try:
            import edge_tts  # noqa: F401
        except ImportError:
            job.status = "failed"
            job.error = "Chưa cài thư viện edge-tts."
            job.message = job.error
            job.save()
            return

        d = job.job_dir()
        clips_dir = d / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        with open(d / "items.json", "r", encoding="utf-8") as f:
            items: list[dict[str, Any]] = json.load(f)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        completed_set = set(job.completed_items)
        t_start = time.monotonic()
        done_in_session = 0
        state_lock = threading.Lock()

        async def process_all() -> None:
            nonlocal done_in_session
            sem = asyncio.Semaphore(max(1, int(job.concurrency or 6)))

            async def handle_item(idx: int, item: dict[str, Any]) -> None:
                nonlocal done_in_session
                if idx in completed_set or cancel_ev.is_set() or pause_ev.is_set():
                    return

                async with sem:
                    if cancel_ev.is_set() or pause_ev.is_set():
                        return

                    raw_mp3 = clips_dir / f"raw_{idx:06d}.mp3"
                    ready_wav = clips_dir / f"seg_{idx:06d}.wav"

                    ok = await self._synthesize_one(
                        item["text"], job.voice, job.rate, job.pitch, raw_mp3
                    )

                    def finalize_segment() -> None:
                        if job.mode == "srt":
                            if idx + 1 < len(items):
                                target_dur = max(0.1, items[idx + 1]["start"] - item["start"])
                            else:
                                target_dur = max(0.5, item["end"] - item["start"])

                            if not ok:
                                self._make_silence_wav(ffmpeg, target_dur, ready_wav)
                            else:
                                actual_dur = probe_duration(raw_mp3)
                                filters: list[str] = []
                                if job.auto_fit and target_dur > 0.25 and actual_dur > target_dur:
                                    speed = actual_dur / target_dur
                                    filters.append(build_atempo_chain(speed))
                                filters.append("apad")
                                filters.append(f"atrim=0:{target_dur:.4f}")
                                self._run_ffmpeg(
                                    [
                                        str(ffmpeg),
                                        "-y",
                                        "-i",
                                        str(raw_mp3),
                                        "-af",
                                        ",".join(filters),
                                        "-ac",
                                        "2",
                                        "-ar",
                                        "44100",
                                        "-c:a",
                                        "pcm_s16le",
                                        str(ready_wav),
                                    ]
                                )
                        else:
                            if not ok:
                                self._make_silence_wav(ffmpeg, 0.2, ready_wav)
                            else:
                                self._run_ffmpeg(
                                    [
                                        str(ffmpeg),
                                        "-y",
                                        "-i",
                                        str(raw_mp3),
                                        "-ac",
                                        "2",
                                        "-ar",
                                        "44100",
                                        "-c:a",
                                        "pcm_s16le",
                                        str(ready_wav),
                                    ]
                                )
                        raw_mp3.unlink(missing_ok=True)

                    await asyncio.to_thread(finalize_segment)

                    with state_lock:
                        completed_set.add(idx)
                        done_in_session += 1
                        job.completed_items = sorted(completed_set)
                        elapsed = max(0.1, time.monotonic() - t_start)
                        rate_per_sec = done_in_session / elapsed
                        remaining = job.total_items - len(completed_set)
                        job.eta_seconds = round(remaining / rate_per_sec, 1) if rate_per_sec > 0 else None
                        job.progress = round((len(completed_set) / max(1, job.total_items)) * 90.0, 1)
                        job.message = f"Đang đọc 6 luồng: {len(completed_set)}/{job.total_items} câu"
                        if done_in_session % 3 == 0 or remaining == 0:
                            job.save()

            tasks = [asyncio.create_task(handle_item(i, it)) for i, it in enumerate(items)]
            await asyncio.gather(*tasks)

        try:
            loop.run_until_complete(process_all())

            if cancel_ev.is_set():
                job.status = "cancelled"
                job.message = "Đã hủy"
                job.save()
                return
            if pause_ev.is_set():
                job.status = "paused"
                job.message = f"Tạm dừng ở câu {len(job.completed_items)}/{job.total_items}"
                job.save()
                return

            # Step 2: Single-pass sample-accurate FFmpeg concat (fast even for 9-hour / 5,000 cues!)
            job.status = "merging"
            job.eta_seconds = None
            job.message = "Đang ghép nối chuẩn mốc thời gian..."
            job.progress = 94.0
            job.save()

            out_dir = d / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            ext = job.output_format.lower()
            codec_args = {
                "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
                "flac": ["-c:a", "flac"],
                "wav": ["-c:a", "pcm_s16le"],
            }.get(ext, ["-c:a", "libmp3lame", "-b:a", "320k"])

            safe_stem = Path(job.source_name).stem or "TTS_Voice"
            final_path = out_dir / f"{safe_stem}_TTS.{ext}"

            concat_list = clips_dir / "concat.txt"
            silence_gap = clips_dir / "gap_silence.wav"
            if job.mode == "txt" and job.pause_between_txt > 0.02:
                self._make_silence_wav(ffmpeg, job.pause_between_txt, silence_gap)

            with open(concat_list, "w", encoding="utf-8") as f:
                if job.mode == "srt" and items and items[0]["start"] > 0.02:
                    lead_wav = clips_dir / "lead_silence.wav"
                    self._make_silence_wav(ffmpeg, items[0]["start"], lead_wav)
                    esc = str(lead_wav.resolve()).replace("'", "'\\''")
                    f.write(f"file '{esc}'\n")

                for idx in range(len(items)):
                    seg = (clips_dir / f"seg_{idx:06d}.wav").resolve()
                    esc = str(seg).replace("'", "'\\''")
                    f.write(f"file '{esc}'\n")
                    if job.mode == "txt" and job.pause_between_txt > 0.02 and idx < len(items) - 1:
                        s_esc = str(silence_gap.resolve()).replace("'", "'\\''")
                        f.write(f"file '{s_esc}'\n")

            self._run_ffmpeg(
                [
                    str(ffmpeg),
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    *codec_args,
                    str(final_path),
                ]
            )

            shutil.rmtree(clips_dir, ignore_errors=True)
            job.output_file = str(final_path.resolve())
            job.status = "completed"
            job.progress = 100.0
            job.message = "Tạo giọng đọc Edge-TTS hoàn tất!"
            job.save()

        except Exception as exc:
            if cancel_ev.is_set():
                job.status = "cancelled"
                job.message = "Đã hủy"
            elif pause_ev.is_set():
                job.status = "paused"
                job.message = f"Tạm dừng ở câu {len(job.completed_items)}/{job.total_items}"
            else:
                job.status = "failed"
                job.error = str(exc)
                job.message = f"Lỗi: {exc}"
            job.save()
        finally:
            loop.close()


tts_manager = TTSManager()
