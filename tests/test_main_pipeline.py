"""Tests for the production orchestration in main.py.

The audit found that main.py — the module that actually produces the film — had
no test coverage at all. These tests exercise the downloader, the opening gate,
credits formatting, the final-video gate and a real FFmpeg render of `render()`.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import diagnostics
import main

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MEDIA_REQUIRED = bool(FFMPEG and FFPROBE)

SAMPLE_HIT = {
    "source": "Pexels",
    "id": "123",
    "creator": "A Creator",
    "page_url": "https://www.pexels.com/video/123",
    "license": "Pexels License",
    "tags": "rainforest canopy",
    "metadata": "rainforest canopy aerial",
}


@contextmanager
def workspace():
    """Run inside a temporary directory so pipeline files never touch the repo."""
    original = Path.cwd()
    with tempfile.TemporaryDirectory() as temp:
        os.chdir(temp)
        try:
            yield Path(temp)
        finally:
            os.chdir(original)


def make_video(path, seconds=2.0, size="1080x1920", source="testsrc2"):
    if source.startswith("color"):
        graph = f"{source}:s={size}:r=30:d={seconds}"
    else:
        graph = f"{source}=size={size}:rate=30:duration={seconds}"
    subprocess.run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", graph,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def make_audio(path, seconds):
    subprocess.run(
        [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", f"{seconds}",
            str(path),
        ],
        check=True,
    )
    return path


class FakeStream:
    def __init__(self, status=200, body=b"x" * 90000, headers=None):
        self.status_code = status
        self._body = body
        self.headers = dict(headers or {})
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.requests.HTTPError(f"{self.status_code} error")

    def close(self):
        self.closed = True


class DownloadStreamTests(unittest.TestCase):
    def test_successful_download_writes_the_file(self):
        with workspace() as temp:
            destination = temp / "scene_01.mp4"
            with patch("main.requests.get", return_value=FakeStream()) as get:
                main.download_stream("https://media.example/clip.mp4", destination)
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.stat().st_size, 90000)
            self.assertEqual(get.call_count, 1)

    def test_rate_limited_primary_falls_back_to_alternate_rendition(self):
        with workspace() as temp:
            destination = temp / "scene_02.mp4"
            limited = FakeStream(429, headers={"Retry-After": "1"})
            healthy = FakeStream(body=b"y" * 120000)
            with patch("main.requests.get", side_effect=[limited, limited, healthy]), patch(
                "main.time.sleep"
            ) as sleep:
                main.download_stream(
                    "https://media.example/large.mp4",
                    destination,
                    alternate_urls=["https://media.example/medium.mp4"],
                )
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.stat().st_size, 120000)
            self.assertTrue(sleep.called)

    def test_oversized_clip_is_rejected_from_content_length(self):
        with workspace() as temp:
            destination = temp / "scene_03.mp4"
            oversized = FakeStream(headers={"Content-Length": str(main.MAX_CLIP_BYTES + 1)})
            with patch("main.requests.get", return_value=oversized):
                with self.assertRaises(RuntimeError) as caught:
                    main.download_stream("https://media.example/huge.mp4", destination)
            self.assertIn("above the", str(caught.exception))
            self.assertFalse(destination.exists())

    def test_truncated_download_is_rejected(self):
        with workspace() as temp:
            destination = temp / "scene_04.mp4"
            tiny = FakeStream(body=b"z" * 100)
            with patch("main.requests.get", return_value=tiny):
                with self.assertRaises(RuntimeError) as caught:
                    main.download_stream("https://media.example/tiny.mp4", destination)
            self.assertIn("rendition URL", str(caught.exception))
            self.assertFalse(destination.exists())

    def test_missing_url_is_reported_clearly(self):
        with workspace() as temp:
            with self.assertRaises(RuntimeError) as caught:
                main.download_stream("ftp://media.example/x.mp4", temp / "x.mp4")
            self.assertIn("no usable download URL", str(caught.exception))


class CreditsFormattingTests(unittest.TestCase):
    def test_credit_line_is_plain_text_not_a_tuple_repr(self):
        line = main.format_scene_credit(
            1,
            SAMPLE_HIT,
            score=0.81,
            matched=3,
            total=4,
            vision_confidence=0.93,
            vision_reason="Rainforest canopy\nwith cloud",
            vision_provider="Groq (qwen)",
        )
        self.assertIsInstance(line, str)
        self.assertNotIn("('", line)
        self.assertIn("Scene 1 | Pexels", line)
        self.assertIn("Visual verification: Groq (qwen) 0.93", line)
        self.assertIn("Rainforest canopy with cloud", line)
        self.assertEqual(len(line.splitlines()), 1)

    def test_unverified_scene_is_explicit_in_the_credit_line(self):
        line = main.format_scene_credit(
            2, SAMPLE_HIT, score=0.4, matched=1, total=3, vision_confidence=None
        )
        self.assertIn("NOT VERIFIED", line)

    def test_coverr_attribution_is_added_only_for_coverr(self):
        coverr = dict(SAMPLE_HIT, source="Coverr")
        self.assertIn("Attribution: Coverr", main.format_scene_credit(
            3, coverr, score=0.5, matched=1, total=2,
            vision_confidence=0.8, vision_provider="Gemini",
        ))
        self.assertNotIn("Attribution: Coverr", main.format_scene_credit(
            3, SAMPLE_HIT, score=0.5, matched=1, total=2,
            vision_confidence=0.8, vision_provider="Gemini",
        ))


class OpeningGateTests(unittest.TestCase):
    def _plan(self, title="", hook=""):
        return {
            "title": title,
            "hook": hook,
            "script": "The forest makes its own rain. Every tree releases moisture.",
            "scenes": [],
        }

    def test_valid_title_and_hook_are_kept(self):
        with workspace() as temp:
            with patch.object(main, "OUT", temp):
                plan = self._plan("The Forest That Makes Rain", "It makes its own weather.")
                layout = main.prepare_opening(plan)
            self.assertTrue(Path(layout["path"]).is_file())
            self.assertEqual(plan["hook"], "It makes its own weather.")
            self.assertEqual(plan["title"], "The Forest That Makes Rain")

    def test_missing_hook_and_title_are_derived_from_the_script(self):
        with workspace() as temp:
            with patch.object(main, "OUT", temp):
                plan = self._plan()
                main.prepare_opening(plan)
            self.assertTrue(plan["hook"])
            self.assertTrue(plan["title"])
            self.assertLessEqual(len(plan["hook"].split()), main.MAX_HOOK_WORDS)

    def test_oversized_hook_is_shortened_before_rendering(self):
        with workspace() as temp:
            with patch.object(main, "OUT", temp):
                plan = self._plan("Title", " ".join(["rain"] * 60))
                layout = main.prepare_opening(plan)
            self.assertLessEqual(len(plan["hook"].split()), main.MAX_HOOK_WORDS)
            self.assertTrue(layout["adjustments"])
            self.assertTrue(Path(layout["path"]).is_file())

    def test_rendering_uses_the_prepared_hook(self):
        with workspace() as temp:
            with patch.object(main, "OUT", temp):
                plan = self._plan("", "")
                main.prepare_opening(plan)
                overlay = main.OUT / "opening_hook_overlay.png"
                from hook_overlay import create_hook_overlay
                create_hook_overlay(plan["title"], plan["hook"], overlay, main.W, main.H)
            self.assertTrue(overlay.is_file())


class SceneTimingTests(unittest.TestCase):
    def test_scene_spans_are_contiguous_and_cover_the_whole_voiceover(self):
        plan = {"scenes": [{"sentence_start": i} for i in range(3)]}
        events = [{"start": 0.0}, {"start": 5.0}, {"start": 10.0}]
        spans = main.scene_durations(plan, events, 15.0)
        self.assertEqual(spans[0][0], 0.0)
        self.assertAlmostEqual(spans[0][1], 5.0)
        self.assertAlmostEqual(spans[1][1], 10.0)
        self.assertAlmostEqual(spans[2][1], 15.0)
        for _start, _end, duration in spans:
            self.assertGreater(duration, 0)


@unittest.skipUnless(MEDIA_REQUIRED, "FFmpeg/ffprobe required")
class FinalVideoGateTests(unittest.TestCase):
    def _encode(self, path, seconds=6.0, with_audio=True):
        command = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=1080x1920:rate=30:duration={seconds}",
        ]
        if with_audio:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        command += [
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-r", "30",
        ]
        if with_audio:
            command += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        command.append(str(path))
        subprocess.run(command, check=True)

    def _support_files(self, out):
        out.mkdir(parents=True, exist_ok=True)
        make_audio(out / "voiceover.mp3", 6.0)
        (out / "captions.ass").write_text("[Events]\n" + ("Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,Hello world\n" * 4), encoding="utf-8")

    def test_complete_video_passes(self):
        with workspace() as temp:
            out = temp / "output"
            self._support_files(out)
            video = temp / "documentary.mp4"
            self._encode(video)
            with patch.object(main, "OUT", out):
                duration = main.validate_final_video(video, 6.0)
            self.assertAlmostEqual(duration, 6.0, delta=0.3)

    def test_duration_mismatch_fails_even_though_the_file_is_valid(self):
        with workspace() as temp:
            out = temp / "output"
            self._support_files(out)
            video = temp / "documentary.mp4"
            self._encode(video, seconds=5.0)
            with patch.object(main, "OUT", out):
                with self.assertRaises(RuntimeError) as caught:
                    main.validate_final_video(video, 6.0)
            self.assertIn("does not match the locked narration", str(caught.exception))

    def test_video_without_audio_fails(self):
        with workspace() as temp:
            out = temp / "output"
            self._support_files(out)
            video = temp / "documentary.mp4"
            self._encode(video, with_audio=False)
            with patch.object(main, "OUT", out):
                with self.assertRaises(RuntimeError) as caught:
                    main.validate_final_video(video, 6.0)
            self.assertIn("video and audio", str(caught.exception))


@unittest.skipUnless(MEDIA_REQUIRED, "FFmpeg/ffprobe required")
class StaticClipTests(unittest.TestCase):
    def test_moving_footage_is_not_flagged(self):
        with workspace() as temp:
            clip = make_video(temp / "moving.mp4", seconds=3.0, size="640x360")
            self.assertLess(main.static_clip_ratio(clip), 0.5)

    def test_frozen_video_is_flagged(self):
        with workspace() as temp:
            clip = make_video(temp / "frozen.mp4", seconds=3.0, size="640x360", source="color=c=navy")
            self.assertGreaterEqual(main.static_clip_ratio(clip), 0.65)


@unittest.skipUnless(MEDIA_REQUIRED, "FFmpeg/ffprobe required")
class RenderPipelineTests(unittest.TestCase):
    """A real FFmpeg render through `render()`, at a reduced frame size."""

    def test_render_produces_a_valid_mp4_with_full_narration(self):
        with workspace() as temp:
            out = temp / "output"
            footage = out / "footage"
            footage.mkdir(parents=True)
            out.mkdir(parents=True, exist_ok=True)

            scenes = []
            for index in range(1, 7):
                clip = make_video(footage / f"scene_{index:02d}.mp4", seconds=2.0, size="540x960")
                scenes.append({
                    "narration": f"Scene {index} narration.",
                    "media_file": str(clip),
                    "visual_type": "footage",
                    "sentence_start": index - 1,
                })

            total_duration = 9.6
            make_audio(out / "voiceover.wav", total_duration)
            make_audio(out / "voiceover.mp3", total_duration)
            lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 540", "PlayResY: 960", "[Events]"]
            for index in range(6):
                start = index * 1.6
                lines.append(
                    f"Dialogue: 0,0:00:{start:05.2f},0:00:{start + 1.4:05.2f},Default,,0,0,0,,Scene {index + 1} caption"
                )
            (out / "captions.ass").write_text("\n".join(lines), encoding="utf-8")
            assert (out / "captions.ass").stat().st_size > 100

            plan = {
                "title": "Test Documentary",
                "hook": "This is only a test.",
                "scenes": scenes,
            }
            events = [{"start": index * 1.6, "end": index * 1.6 + 1.5} for index in range(6)]

            with patch.object(main, "W", 540), patch.object(main, "H", 960), \
                    patch.object(main, "OUT", out), patch.object(main, "FOOT", footage), \
                    patch.object(main, "X264_PRESET", "ultrafast"):
                main.render(plan, events, total_duration)

            final = temp / "documentary.mp4"
            self.assertTrue(final.is_file())
            probe = subprocess.run(
                [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(final)],
                capture_output=True, text=True, check=True,
            )
            details = json.loads(probe.stdout)
            streams = details["streams"]
            video = next(s for s in streams if s["codec_type"] == "video")
            audio = next(s for s in streams if s["codec_type"] == "audio")
            self.assertEqual((video["width"], video["height"]), (540, 960))
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(audio["codec_name"], "aac")
            format_duration = float(details["format"]["duration"])
            self.assertAlmostEqual(format_duration, total_duration, delta=main.DURATION_TOLERANCE_SECONDS)
            # The narration must not be cut by the mux.
            self.assertAlmostEqual(
                float(audio["duration"]), total_duration, delta=main.DURATION_TOLERANCE_SECONDS
            )


class DiagnosticsTests(unittest.TestCase):
    def test_stage_timings_are_recorded(self):
        log = diagnostics.StageLog()
        with log.stage("first"):
            pass
        log.begin("second")
        log.complete(extra="value")
        summary = log.summary()
        self.assertEqual([item["stage"] for item in summary["stages"]], ["first", "second"])
        self.assertTrue(all(item["status"] == "completed" for item in summary["stages"]))
        self.assertIsNone(summary["failed_stage"])
        self.assertEqual(summary["stages"][1]["details"]["extra"], "value")

    def test_failure_report_redacts_credentials_and_names_the_stage(self):
        with workspace() as temp, patch.dict(os.environ, {"DEMO_API_KEY": "super-secret-value"}, clear=False):
            diagnostics.STAGES._stages.clear()
            diagnostics.STAGES.begin("render")
            try:
                raise RuntimeError("provider rejected super-secret-value")
            except RuntimeError as exc:
                report = diagnostics.write_failure_report("failure_report.json", exc)
            payload = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(payload["failed_stage"], "render")
            self.assertEqual(payload["status"], "failed")
            self.assertNotIn("super-secret-value", json.dumps(payload))
            self.assertIn("[REDACTED]", payload["error_message"])

    def test_main_writes_diagnostics_when_the_run_cannot_start(self):
        with workspace() as temp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INPUT_TOPIC", None)
            with patch.object(main, "TOPIC", ""):
                with self.assertRaises(RuntimeError):
                    main.main()
            self.assertTrue((temp / "failure_report.json").is_file())
            self.assertTrue((temp / "stage_report.json").is_file())
            payload = json.loads((temp / "failure_report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertIn("topic", payload["error_message"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
