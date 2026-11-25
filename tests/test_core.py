"""Core unit tests for VocalForge chunking, export builder, and API endpoints."""

from __future__ import annotations

import unittest

from backend.audio_engine import build_export_command
from backend.demucs_worker import compute_chunks


class TestVocalForgeCore(unittest.TestCase):
    def test_compute_chunks_9_hours_exact_coverage(self) -> None:
        total = 9 * 3600.0  # 32,400 seconds (9 hours)
        chunks = compute_chunks(total, chunk_duration=300.0, overlap=4.0)

        self.assertEqual(len(chunks), 108)
        self.assertEqual(chunks[0].core_start, 0.0)
        self.assertEqual(chunks[0].extract_start, 0.0)
        self.assertEqual(chunks[0].trim_offset, 0.0)
        self.assertEqual(chunks[0].extract_end, 304.0)

        # Middle chunk has 4s overlap on both sides
        mid = chunks[50]
        self.assertEqual(mid.trim_offset, 4.0)
        self.assertEqual(mid.extract_start, mid.core_start - 4.0)
        self.assertEqual(mid.extract_end, mid.core_end + 4.0)

        # Last chunk ends exactly at 9 hours
        last = chunks[-1]
        self.assertEqual(last.core_end, total)
        self.assertEqual(last.extract_end, total)

        # Contiguous, non-overlapping cores sum to exact total duration
        covered = 0.0
        for i, c in enumerate(chunks):
            self.assertAlmostEqual(c.core_start, covered, places=4)
            covered += c.core_duration
            self.assertEqual(c.index, i)
        self.assertAlmostEqual(covered, total, places=4)

    def test_compute_chunks_short_file(self) -> None:
        chunks = compute_chunks(42.5, chunk_duration=300.0, overlap=4.0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].core_start, 0.0)
        self.assertEqual(chunks[0].core_end, 42.5)
        self.assertEqual(chunks[0].trim_offset, 0.0)

    def test_build_export_command_multitrack(self) -> None:
        clips = [
            {
                "path": "/tmp/vocals.mp3",
                "startTime": 0.0,
                "trimStart": 10.0,
                "trimEnd": 70.0,
                "volume": 1.2,
                "fadeIn": 1.5,
                "fadeOut": 2.0,
            },
            {
                "path": "/tmp/bgm.mp3",
                "startTime": 5.5,
                "trimStart": 0.0,
                "trimEnd": 120.0,
                "volume": 0.6,
                "fadeIn": 0.0,
                "fadeOut": 3.0,
            },
        ]
        cmd, total_dur = build_export_command(clips, "/tmp/out.mp3", "mp3", "ffmpeg")
        self.assertAlmostEqual(total_dur, 125.5, places=3)
        fc_idx = cmd.index("-filter_complex")
        fc = cmd[fc_idx + 1]
        self.assertIn("atrim=start=10.0000:end=70.0000", fc)
        self.assertIn("afade=t=in:st=0:d=1.5000", fc)
        self.assertIn("adelay=5500|5500", fc)
        self.assertIn("amix=inputs=2:duration=longest:dropout_transition=0:normalize=0", fc)
        self.assertIn("libmp3lame", cmd)

    def test_parse_srt_and_txt(self) -> None:
        from backend.tts_worker import parse_srt, parse_txt

        srt = (
            "1\n"
            "00:00:01,500 --> 00:00:04,250\n"
            "<i>Xin chào</i> các bạn.\n\n"
            "2\n"
            "01:02:03,000 --> 01:02:06,500\n"
            "Đây là câu thứ hai\ntrên hai dòng.\n"
        )
        cues = parse_srt(srt)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0]["start"], 1.5, places=3)
        self.assertAlmostEqual(cues[0]["end"], 4.25, places=3)
        self.assertEqual(cues[0]["text"], "Xin chào các bạn.")
        self.assertAlmostEqual(cues[1]["start"], 3723.0, places=3)
        self.assertEqual(cues[1]["text"], "Đây là câu thứ hai trên hai dòng.")

        txt = "Đoạn một rất ngắn.\n\n" + ("Câu dài lặp lại nhiều lần. " * 25)
        segs = parse_txt(txt, max_chars=200)
        self.assertGreaterEqual(len(segs), 3)
        self.assertEqual(segs[0]["text"], "Đoạn một rất ngắn.")
        for seg in segs:
            self.assertLessEqual(len(seg["text"]), 200)

    def test_khmer_tts_voices_and_splitting(self) -> None:
        from backend.tts_worker import TTS_VOICES, parse_txt

        voice_ids = {v["id"] for v in TTS_VOICES}
        self.assertIn("km-KH-SreymomNeural", voice_ids)
        self.assertIn("km-KH-PisethNeural", voice_ids)

        khmer_long = "សួស្តីអ្នកទាំងអស់គ្នា នេះជាការសាកល្បងអានភាសាខ្មែរ។" * 12
        segs = parse_txt(khmer_long, max_chars=120)
        self.assertGreaterEqual(len(segs), 3)
        for seg in segs:
            self.assertLessEqual(len(seg["text"]), 120)
            self.assertTrue(seg["text"].endswith("។"))


if __name__ == "__main__":
    unittest.main()
