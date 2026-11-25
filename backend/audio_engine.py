"""Lightweight waveform peak generator and FFmpeg multitrack mix/export engine."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from backend.config import CACHE_DIR, get_ffmpeg_path, hidden_subprocess_kwargs
from backend.demucs_worker import probe_duration


def _peak_cache_path(file_path: Path, target_points: int) -> Path:
    st = file_path.stat()
    key = f"{file_path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{target_points}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"peaks_{digest}.json"


def generate_waveform_peaks(file_path: str | Path, target_points: int = 2400) -> dict[str, Any]:
    """Compute lightweight normalized amplitude peaks via streaming FFmpeg PCM pipe.

    Handles 9+ hour files with constant low RAM (~1 MB) and caches the result on disk.
    """
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(f"Không tìm thấy file audio: {file_path}")

    cache_file = _peak_cache_path(src, target_points)
    if cache_file.is_file():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    duration = probe_duration(src)
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("Chưa có FFmpeg.")

    # Use very low sample rate for ultra-fast streaming peak extraction on long files
    sample_rate = 1000 if duration > 3600 else (2000 if duration > 600 else 4000)
    total_samples = max(1, int(duration * sample_rate))
    samples_per_bucket = max(1, math.ceil(total_samples / target_points))

    cmd = [
        str(ffmpeg),
        "-v",
        "error",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )

    peaks: list[float] = []
    bucket_max = 0
    bucket_count = 0
    leftover = b""

    assert proc.stdout is not None
    while True:
        raw = proc.stdout.read(65536)
        if not raw:
            break
        buf = leftover + raw
        usable = len(buf) - (len(buf) % 2)
        leftover = buf[usable:]
        if usable == 0:
            continue

        sample_count = usable // 2
        samples = struct.unpack(f"<{sample_count}h", buf[:usable])
        for s in samples:
            val = -s if s < 0 else s
            if val > bucket_max:
                bucket_max = val
            bucket_count += 1
            if bucket_count >= samples_per_bucket:
                peaks.append(round(min(1.0, bucket_max / 32768.0), 3))
                bucket_max = 0
                bucket_count = 0

    if bucket_count > 0:
        peaks.append(round(min(1.0, bucket_max / 32768.0), 3))

    proc.wait()

    result = {
        "path": str(src.resolve()),
        "name": src.name,
        "duration": round(duration, 3),
        "peaks": peaks or [0.0],
    }
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(result, f)
    except Exception:
        pass
    return result


def build_export_command(
    clips: list[dict[str, Any]],
    output_path: str | Path,
    output_format: str = "mp3",
    ffmpeg_path: str | Path = "ffmpeg",
) -> tuple[list[str], float]:
    """Build FFmpeg filter_complex command to mix edited multitrack clips.

    Returns (command_list, estimated_total_duration_seconds).
    """
    if not clips:
        raise ValueError("Không có clip nào để xuất file.")

    inputs: list[str] = []
    filter_parts: list[str] = []
    mix_labels: list[str] = []
    max_end_time = 0.0

    for idx, clip in enumerate(clips):
        src = str(clip["path"])
        start_time = max(0.0, float(clip.get("startTime", 0.0)))
        trim_start = max(0.0, float(clip.get("trimStart", 0.0)))
        trim_end = float(clip.get("trimEnd", 0.0))
        volume = max(0.0, min(3.0, float(clip.get("volume", 1.0))))
        fade_in = max(0.0, float(clip.get("fadeIn", 0.0)))
        fade_out = max(0.0, float(clip.get("fadeOut", 0.0)))

        clip_dur = max(0.05, trim_end - trim_start)
        end_time = start_time + clip_dur
        if end_time > max_end_time:
            max_end_time = end_time

        inputs.extend(["-i", src])
        chain: list[str] = [
            f"atrim=start={trim_start:.4f}:end={trim_end:.4f}",
            "asetpts=PTS-STARTPTS",
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
        ]
        if fade_in > 0.01:
            fi = min(fade_in, clip_dur / 2.0)
            chain.append(f"afade=t=in:st=0:d={fi:.4f}")
        if fade_out > 0.01:
            fo = min(fade_out, clip_dur / 2.0)
            fo_start = max(0.0, clip_dur - fo)
            chain.append(f"afade=t=out:st={fo_start:.4f}:d={fo:.4f}")
        if abs(volume - 1.0) > 0.001:
            chain.append(f"volume={volume:.4f}")

        delay_ms = int(round(start_time * 1000.0))
        if delay_ms > 0:
            chain.append(f"adelay={delay_ms}|{delay_ms}")

        label = f"a{idx}"
        filter_parts.append(f"[{idx}:a]{','.join(chain)}[{label}]")
        mix_labels.append(f"[{label}]")

    if len(mix_labels) == 1:
        filter_parts.append(f"{mix_labels[0]}alimiter=limit=0.98[out]")
    else:
        joined = "".join(mix_labels)
        filter_parts.append(
            f"{joined}amix=inputs={len(mix_labels)}:duration=longest:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.98[out]"
        )

    ext = output_format.lower()
    codec_args = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "flac": ["-c:a", "flac"],
        "wav": ["-c:a", "pcm_s16le"],
    }.get(ext, ["-c:a", "libmp3lame", "-b:a", "320k"])

    cmd = [
        str(ffmpeg_path),
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[out]",
        *codec_args,
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]
    return cmd, max_end_time


@dataclass
class ExportTask:
    id: str
    output_path: str
    status: str = "running"  # running, completed, failed
    progress: float = 0.0
    message: str = "Đang xuất file..."
    error: str | None = None
    created_at: float = 0.0


class ExportManager:
    def __init__(self) -> None:
        self._tasks: dict[str, ExportTask] = {}
        self._lock = threading.Lock()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return asdict(task) if task else None

    def start_export(
        self,
        clips: list[dict[str, Any]],
        output_path: str,
        output_format: str = "mp3",
    ) -> dict[str, Any]:
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("Chưa có FFmpeg.")

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd, total_dur = build_export_command(clips, out_path, output_format, ffmpeg)

        task_id = uuid.uuid4().hex[:10]
        task = ExportTask(
            id=task_id,
            output_path=str(out_path.resolve()),
            created_at=time.time(),
        )
        with self._lock:
            self._tasks[task_id] = task

        t = threading.Thread(
            target=self._run_export,
            args=(task, cmd, total_dur),
            daemon=True,
        )
        t.start()
        return asdict(task)

    def _run_export(self, task: ExportTask, cmd: list[str], total_dur: float) -> None:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_kwargs(),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                        sec = us / 1_000_000.0
                        if total_dur > 0:
                            task.progress = round(min(99.0, (sec / total_dur) * 100.0), 1)
                            task.message = f"Đang xuất file... {task.progress:.0f}%"
                    except ValueError:
                        pass

            _, stderr = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(stderr.strip()[-400:] or f"FFmpeg thoát với mã lỗi {proc.returncode}")

            task.status = "completed"
            task.progress = 100.0
            task.message = "Xuất file thành công!"
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.message = f"Lỗi xuất file: {exc}"

    def start_separate_tracks_export(
        self,
        tracks: list[dict[str, Any]],
        output_dir: str,
        output_format: str = "mp3",
    ) -> dict[str, Any]:
        """Export each active track as its own edited audio file in output_dir."""
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("Chưa có FFmpeg.")

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        valid_tracks = [t for t in tracks if t.get("clips")]
        if not valid_tracks:
            raise ValueError("Không có track nào có clip để xuất.")

        task_id = uuid.uuid4().hex[:10]
        task = ExportTask(
            id=task_id,
            output_path=str(out_dir.resolve()),
            created_at=time.time(),
            message=f"Đang xuất riêng {len(valid_tracks)} track...",
        )
        with self._lock:
            self._tasks[task_id] = task

        t = threading.Thread(
            target=self._run_separate_export,
            args=(task, valid_tracks, out_dir, output_format, ffmpeg),
            daemon=True,
        )
        t.start()
        return asdict(task)

    def _run_separate_export(
        self,
        task: ExportTask,
        tracks: list[dict[str, Any]],
        out_dir: Path,
        output_format: str,
        ffmpeg: Path,
    ) -> None:
        try:
            ext = output_format.lower().strip(".")
            total_tracks = len(tracks)
            for idx, tr in enumerate(tracks):
                raw_name = str(tr.get("name") or f"Track_{idx + 1}")
                safe = "".join(c for c in Path(raw_name).stem if c.isalnum() or c in ("-", "_", " ")).strip()
                out_file = out_dir / f"{idx + 1:02d}_{safe or 'Track'}.{ext}"
                cmd, total_dur = build_export_command(tr["clips"], out_file, ext, ffmpeg)
                task.message = f"Đang xuất track {idx + 1}/{total_tracks}: {raw_name}"

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **hidden_subprocess_kwargs(),
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time_us=") and total_dur > 0:
                        try:
                            sec = int(line.split("=", 1)[1]) / 1_000_000.0
                            tr_pct = min(1.0, sec / total_dur)
                            overall = ((idx + tr_pct) / total_tracks) * 99.0
                            task.progress = round(overall, 1)
                        except ValueError:
                            pass
                _, stderr = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(stderr.strip()[-300:] or f"Lỗi xuất track {raw_name}")

            task.status = "completed"
            task.progress = 100.0
            task.message = f"Đã xuất xong {total_tracks} track riêng biệt!"
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.message = f"Lỗi xuất file: {exc}"

    def start_video_export(
        self,
        clips: list[dict[str, Any]],
        video_source_path: str,
        output_path: str,
        resolution: str = "original",
        fps: str = "original",
        overlays: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Mux edited multitrack audio and optional logo/sticker overlays onto original video.

        Uses lossless stream copy (-c:v copy) when resolution & fps are 'original' and no
        overlays are applied; otherwise uses hardware-accelerated h264_nvenc (fallback libx264).
        """
        from backend.config import detect_nvidia_gpu

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("Chưa có FFmpeg.")
        vid = Path(video_source_path)
        if not vid.is_file():
            raise FileNotFoundError(f"Không tìm thấy video gốc: {video_source_path}")

        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        valid_overlays = [o for o in (overlays or []) if o.get("path") and Path(o["path"]).is_file()]

        inputs: list[str] = ["-i", str(vid)]
        filter_parts: list[str] = []
        mix_labels: list[str] = []
        max_end_time = 0.0

        # Audio clip inputs start at index 1
        for idx, clip in enumerate(clips):
            in_idx = idx + 1
            src = str(clip["path"])
            start_time = max(0.0, float(clip.get("startTime", 0.0)))
            trim_start = max(0.0, float(clip.get("trimStart", 0.0)))
            trim_end = float(clip.get("trimEnd", 0.0))
            volume = max(0.0, min(3.0, float(clip.get("volume", 1.0))))
            fade_in = max(0.0, float(clip.get("fadeIn", 0.0)))
            fade_out = max(0.0, float(clip.get("fadeOut", 0.0)))

            clip_dur = max(0.05, trim_end - trim_start)
            max_end_time = max(max_end_time, start_time + clip_dur)

            inputs.extend(["-i", src])
            chain = [
                f"atrim=start={trim_start:.4f}:end={trim_end:.4f}",
                "asetpts=PTS-STARTPTS",
                "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
            ]
            if fade_in > 0.01:
                chain.append(f"afade=t=in:st=0:d={min(fade_in, clip_dur / 2.0):.4f}")
            if fade_out > 0.01:
                fo = min(fade_out, clip_dur / 2.0)
                chain.append(f"afade=t=out:st={max(0.0, clip_dur - fo):.4f}:d={fo:.4f}")
            if abs(volume - 1.0) > 0.001:
                chain.append(f"volume={volume:.4f}")
            delay_ms = int(round(start_time * 1000.0))
            if delay_ms > 0:
                chain.append(f"adelay={delay_ms}|{delay_ms}")

            label = f"a{idx}"
            filter_parts.append(f"[{in_idx}:a]{','.join(chain)}[{label}]")
            mix_labels.append(f"[{label}]")

        if len(mix_labels) == 1:
            filter_parts.append(f"{mix_labels[0]}alimiter=limit=0.98[out]")
        else:
            joined = "".join(mix_labels)
            filter_parts.append(
                f"{joined}amix=inputs={len(mix_labels)}:duration=longest:dropout_transition=0:normalize=0,"
                f"alimiter=limit=0.98[out]"
            )

        need_video_reencode = (resolution != "original") or (fps != "original") or bool(valid_overlays)
        video_map = "0:v:0"
        video_args: list[str] = []

        if not need_video_reencode:
            video_args = ["-c:v", "copy"]
        else:
            # Build video filter graph: scale/fps first, then overlay each logo/sticker
            v_curr = "0:v:0"
            vf_pre: list[str] = []
            if resolution in ("720", "1080", "1440", "2160"):
                vf_pre.append(f"scale=-2:{resolution}")
            if fps in ("24", "30", "60"):
                vf_pre.append(f"fps={fps}")
            if vf_pre:
                filter_parts.append(f"[{v_curr}]{','.join(vf_pre)}[vbase]")
                v_curr = "vbase"

            ov_base_idx = 1 + len(clips)
            for o_idx, ov in enumerate(valid_overlays):
                in_idx = ov_base_idx + o_idx
                inputs.extend(["-i", str(ov["path"])])
                x_pct = max(0.0, min(100.0, float(ov.get("xPct", 5.0)))) / 100.0
                y_pct = max(0.0, min(100.0, float(ov.get("yPct", 5.0)))) / 100.0
                w_pct = max(2.0, min(100.0, float(ov.get("widthPct", 16.0)))) / 100.0
                opacity = max(0.05, min(1.0, float(ov.get("opacity", 1.0))))

                ov_scaled = f"ov_s{o_idx}"
                v_ref = f"vref{o_idx}"
                v_next = f"vout{o_idx}"
                alpha_f = f",format=rgba,colorchannelmixer=aa={opacity:.3f}" if opacity < 0.99 else ",format=rgba"
                filter_parts.append(
                    f"[{in_idx}:v][{v_curr}]scale2ref=w=main_w*{w_pct:.4f}:h=-1[{ov_scaled}_raw][{v_ref}]"
                )
                filter_parts.append(f"[{ov_scaled}_raw]setsar=1{alpha_f}[{ov_scaled}]")
                filter_parts.append(
                    f"[{v_ref}][{ov_scaled}]overlay=x=W*{x_pct:.4f}:y=H*{y_pct:.4f}:shortest=0[{v_next}]"
                )
                v_curr = v_next

            if v_curr != "0:v:0":
                video_map = f"[{v_curr}]"

            if detect_nvidia_gpu().get("has_gpu"):
                video_args.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-pix_fmt", "yuv420p"])
            else:
                video_args.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"])

        cmd = [
            str(ffmpeg),
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            video_map,
            "-map",
            "[out]",
            *video_args,
            "-c:a",
            "aac",
            "-b:a",
            "320k",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(out_path),
        ]

        task_id = uuid.uuid4().hex[:10]
        mode_desc = "Copy gốc" if not need_video_reencode else f"{resolution}p / {fps}fps"
        if valid_overlays:
            mode_desc += f" + {len(valid_overlays)} logo"
        task = ExportTask(
            id=task_id,
            output_path=str(out_path.resolve()),
            created_at=time.time(),
            message=f"Đang xuất Video MP4 ({mode_desc})...",
        )
        with self._lock:
            self._tasks[task_id] = task

        t = threading.Thread(
            target=self._run_export,
            args=(task, cmd, max(max_end_time, probe_duration(vid))),
            daemon=True,
        )
        t.start()
        return asdict(task)


export_manager = ExportManager()
