import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main


class MM5AdaptiveDownloadTests(unittest.TestCase):
    def test_default_download_budget_remains_180_mb(self):
        self.assertEqual(main.candidate_download_limit(0.60, {}), 180 * 1024 * 1024)

    def test_relevant_landscape_can_use_650_mb_budget(self):
        self.assertEqual(main.candidate_download_limit(0.76, {}), 650 * 1024 * 1024)

    def test_exceptionally_relevant_candidate_can_use_900_mb_budget(self):
        self.assertEqual(main.candidate_download_limit(0.91, {}), 900 * 1024 * 1024)

    def test_known_file_size_above_relevance_budget_is_rejected_before_download(self):
        with self.assertRaisesRegex(RuntimeError, "above its relevance-based"):
            main.candidate_download_limit(0.76, {"file_size": 700 * 1024 * 1024})

    def test_known_550_mb_file_is_allowed_for_high_relevance_candidate(self):
        limit = main.candidate_download_limit(0.80, {"file_size": 550 * 1024 * 1024})
        self.assertGreaterEqual(limit, 550 * 1024 * 1024)

    def test_downloader_streams_file_larger_than_small_custom_limit(self):
        payload = b"x" * 60000

        class FakeResponse:
            status_code = 200
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _chunk_size):
                yield payload[:30000]
                yield payload[30000:]

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "clip.mp4"
            with patch.object(main, "_footage_source_get", return_value=FakeResponse()):
                main.download_stream(
                    "https://example.test/clip.mp4", target,
                    source="Internet Archive", max_bytes=100000,
                )
            self.assertEqual(target.stat().st_size, 60000)
            self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_downloader_resumes_partial_file_using_http_range(self):
        payload = b"a" * 1000 + b"b" * 59000

        class FakeResponse:
            status_code = 206
            headers = {
                "Content-Length": "59000",
                "Content-Range": "bytes 1000-59999/60000",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _chunk_size):
                yield payload[1000:]

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "clip.mp4"
            part = target.with_name(target.name + ".part")
            part.write_bytes(payload[:1000])
            with patch.object(main, "_footage_source_get", return_value=FakeResponse()) as get:
                main.download_stream(
                    "https://example.test/clip.mp4", target,
                    source="NASA", max_bytes=100000,
                )
            self.assertEqual(target.read_bytes(), payload)
            headers = get.call_args.kwargs["headers"]
            self.assertEqual(headers["Range"], "bytes=1000-")

    def test_download_limit_removes_oversized_partial_file(self):
        payload = b"x" * 60000

        class FakeResponse:
            status_code = 200
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _chunk_size):
                yield payload

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "clip.mp4"
            with patch.object(main, "_footage_source_get", return_value=FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, "above the"):
                    main.download_stream(
                        "https://example.test/clip.mp4", target,
                        source="NASA", max_bytes=55000,
                    )
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(target.name + ".part").exists())


class MM5SmartCropGeometryTests(unittest.TestCase):
    def setUp(self):
        self.source_info = {
            "width": 1920,
            "height": 1080,
            "aspect_ratio": 16 / 9,
            "rotation_degrees": 0,
        }
        self.frame_times = [1.0, 2.5, 4.0, 5.5, 7.0, 8.5]
        centers = [0.34, 0.36, 0.39, 0.42, 0.46, 0.49]
        self.payload = {
            "subject": "tiger walking through the forest",
            "subject_visible": True,
            "composition_fits": True,
            "reason": "The tiger remains fully inside a moving portrait window.",
            "frames": [
                {
                    "bbox": [center - 0.055, 0.18, center + 0.055, 0.82],
                    "confidence": 0.92,
                }
                for center in centers
            ],
        }

    def test_16_9_landscape_is_eligible_for_smart_reframe(self):
        self.assertTrue(main.is_smart_reframe_eligible(1920, 1080, 16 / 9))

    def test_native_portrait_is_not_sent_through_landscape_reframe(self):
        self.assertFalse(main.is_smart_reframe_eligible(1080, 1920, 9 / 16))
        self.assertTrue(main.is_native_portrait_916(1080, 1920))

    def test_square_and_4_5_sources_are_not_accepted_as_landscape_crop(self):
        self.assertFalse(main.is_smart_reframe_eligible(1080, 1080, 1.0))
        self.assertFalse(main.is_smart_reframe_eligible(1080, 1350, 0.8))

    def test_low_resolution_landscape_is_rejected(self):
        self.assertFalse(main.is_smart_reframe_eligible(1280, 600, 1280 / 600))

    def test_valid_boxes_produce_smooth_dynamic_crop_plan(self):
        plan = main.validate_smart_crop_plan(self.payload, self.source_info, self.frame_times)
        self.assertEqual(plan["mode"], "ai_content_aware_dynamic_crop")
        self.assertEqual(len(plan["points"]), 6)
        self.assertTrue(all(0 <= point["x"] <= 1 - plan["crop_width_fraction"] for point in plan["points"]))
        self.assertGreater(plan["points"][-1]["x"], plan["points"][0]["x"])

    def test_content_too_wide_to_fit_9_16_is_rejected(self):
        payload = json.loads(json.dumps(self.payload))
        payload["frames"][2]["bbox"] = [0.15, 0.1, 0.75, 0.9]
        with self.assertRaisesRegex(RuntimeError, "too wide"):
            main.validate_smart_crop_plan(payload, self.source_info, self.frame_times)

    def test_ai_must_explicitly_confirm_subject_and_composition(self):
        payload = json.loads(json.dumps(self.payload))
        payload["composition_fits"] = False
        with self.assertRaisesRegex(RuntimeError, "cannot be kept"):
            main.validate_smart_crop_plan(payload, self.source_info, self.frame_times)

    def test_low_confidence_or_invalid_boxes_are_rejected(self):
        payload = json.loads(json.dumps(self.payload))
        payload["frames"][0]["confidence"] = 0.20
        with self.assertRaisesRegex(RuntimeError, "confidence is too low"):
            main.validate_smart_crop_plan(payload, self.source_info, self.frame_times)

        payload = json.loads(json.dumps(self.payload))
        payload["frames"][0]["bbox"] = [-0.1, 0.1, 0.2, 0.8]
        with self.assertRaisesRegex(RuntimeError, "out-of-range"):
            main.validate_smart_crop_plan(payload, self.source_info, self.frame_times)

    def test_missing_box_for_any_sampled_frame_is_rejected(self):
        payload = json.loads(json.dumps(self.payload))
        payload["frames"].pop()
        with self.assertRaisesRegex(RuntimeError, "one bounding box for every"):
            main.validate_smart_crop_plan(payload, self.source_info, self.frame_times)

    def test_crop_window_keeps_safe_even_dimensions_and_dynamic_expression(self):
        plan = main.validate_smart_crop_plan(self.payload, self.source_info, self.frame_times)
        plan["source_duration"] = 10.0
        crop_width, crop_height, expression = main.smart_crop_geometry(self.source_info, plan)
        self.assertEqual(crop_width % 2, 0)
        self.assertEqual(crop_height % 2, 0)
        self.assertEqual(crop_height, 1080)
        self.assertAlmostEqual(crop_width / crop_height, 9 / 16, delta=0.002)
        self.assertIn("mod(t,10.0000)", expression)
        self.assertIn("if(lt(mod(t", expression)

    def test_interpolated_crop_position_moves_between_tracking_points(self):
        plan = main.validate_smart_crop_plan(self.payload, self.source_info, self.frame_times)
        at_start = main.crop_x_at_time(plan, 1.0)
        at_middle = main.crop_x_at_time(plan, 4.75)
        at_end = main.crop_x_at_time(plan, 8.5)
        self.assertLess(at_start, at_middle)
        self.assertLess(at_middle, at_end)

    def test_dynamic_crop_filter_has_9_16_crop_and_scaling(self):
        plan = main.validate_smart_crop_plan(self.payload, self.source_info, self.frame_times)
        plan["source_duration"] = 10.0
        crop_filter = main.smart_crop_filter(self.source_info, plan)
        self.assertIn("crop=608:1080", crop_filter)
        self.assertIn("scale=768:-2", crop_filter)

    def test_wrong_source_dimensions_in_crop_plan_are_rejected(self):
        plan = main.validate_smart_crop_plan(self.payload, self.source_info, self.frame_times)
        plan["source_duration"] = 10.0
        wrong_info = dict(self.source_info, width=1280)
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            main.smart_crop_geometry(wrong_info, plan)

    def test_ai_json_parser_accepts_crop_plan_schema_without_visual_verdict_fields(self):
        raw = json.dumps(self.payload)
        parsed = main._parse_visual_json("Test vision", raw)
        self.assertTrue(parsed["composition_fits"])
        self.assertEqual(len(parsed["frames"]), 6)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
class MM5FFmpegReframeTests(unittest.TestCase):
    def test_dynamic_landscape_crop_renders_native_9_16_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            output = root / "vertical.mp4"
            create = __import__("subprocess").run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=12",
                    "-t", "2", "-pix_fmt", "yuv420p", str(source),
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(create.returncode, 0, create.stderr)
            info = main.ffprobe_video_info(source)
            payload = {
                "subject": "moving test chart",
                "subject_visible": True,
                "composition_fits": True,
                "reason": "synthetic unit-test content",
                "frames": [
                    {"bbox": [0.32 + index * 0.015, 0.1, 0.42 + index * 0.015, 0.9], "confidence": 0.95}
                    for index in range(6)
                ],
            }
            frame_times = [0.2, 0.45, 0.7, 0.95, 1.2, 1.5]
            plan = main.validate_smart_crop_plan(payload, info, frame_times)
            plan["source_duration"] = main.ffprobe_duration(source)
            main.render_footage_scene(source, 1.5, output, smart_crop_plan=plan)
            output_info = main.ffprobe_video_info(output)
            self.assertTrue(main.is_native_portrait_916(
                output_info["width"], output_info["height"],
                aspect_ratio=output_info["aspect_ratio"],
            ))

    def test_landscape_render_without_crop_plan_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            output = root / "vertical.mp4"
            create = __import__("subprocess").run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=10",
                    "-t", "1", "-pix_fmt", "yuv420p", str(source),
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(create.returncode, 0, create.stderr)
            with self.assertRaisesRegex(RuntimeError, "without a verified AI smart-crop plan"):
                main.render_footage_scene(source, 1.0, output)


class MM5SelectionIntegrationTests(unittest.TestCase):
    def _run_landscape_candidate(self, crop_verdict=True):
        from unittest.mock import patch
        scene = {
            "visual_query": "tiger walking in rainforest",
            "visual_queries": [],
            "narration": "A tiger moves quietly through its forest habitat.",
        }
        hit = {
            "source": "Internet Archive",
            "id": "landscape-tiger-test",
            "url": "https://example.test/tiger.mp4",
            "page_url": "https://archive.org/details/landscape-tiger-test",
            "title": "Tiger walking in rainforest documentary footage",
            "tags": "tiger walking rainforest wildlife forest habitat",
            "metadata": "Tiger walking in a natural rainforest habitat",
            "creator": "Test Creator",
            "license": "CC0",
            "width": 1920,
            "height": 1080,
            "duration": 12,
        }
        crop_plan = {
            "mode": "ai_content_aware_dynamic_crop",
            "target_aspect": "9:16",
            "source_width": 1920,
            "source_height": 1080,
            "source_aspect_ratio": 16 / 9,
            "crop_width_fraction": 9 / 16 / (16 / 9),
            "source_duration": 12,
            "subject": "tiger",
            "reason": "The tiger fits in a moving portrait window.",
            "vision_provider": "TestVision",
            "points": [
                {"t": t, "x": 0.20 + index * 0.012, "bbox": [0.25, 0.1, 0.37, 0.9], "confidence": 0.95}
                for index, t in enumerate([1.2, 2.7, 4.2, 5.7, 7.2, 8.7])
            ],
        }
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            try:
                os.chdir(root)
                verification_results = [
                    (True, 0.95, "Original frames show the requested tiger.", "TestVision"),
                    (crop_verdict, 0.91 if crop_verdict else 0.31,
                     "Cropped frames retain the tiger." if crop_verdict else "The tiger leaves the crop.",
                     "TestVision"),
                ]
                def fake_download(_url, dest, source=None, max_bytes=None):
                    Path(dest).parent.mkdir(parents=True, exist_ok=True)
                    Path(dest).write_bytes(b"x" * 60000)

                with patch.object(main, "OUT", root / "output"), \
                     patch.object(main, "FOOT", root / "output" / "footage"), \
                     patch.object(main, "CACHE", root / ".api-cache"), \
                     patch.object(main, "GROQ_KEYS", ["unit-test-key"]), \
                     patch.object(main, "OPENROUTER_KEYS", []), \
                     patch.object(main, "GEMINI_KEYS", []), \
                     patch.object(main, "CLOUDFLARE_CREDENTIALS", []), \
                     patch.object(main, "search_all_footage_sources", return_value=[dict(hit)]), \
                     patch.object(main, "expand_visual_queries", return_value=["tiger walking rainforest"]), \
                     patch.object(main, "candidate_matches_visual_anchors", return_value=True), \
                     patch.object(main, "unrequested_context_conflicts", return_value=()), \
                     patch.object(main, "relevance_score", return_value=(0.80, 3, 3)), \
                     patch.object(main, "download_stream", side_effect=fake_download), \
                     patch.object(main, "ffprobe_video_info", return_value={
                         "width": 1920, "height": 1080, "aspect_ratio": 16 / 9,
                         "sample_aspect_ratio": "1:1", "rotation_degrees": 0,
                     }), \
                     patch.object(main, "verify_footage_visually", side_effect=verification_results), \
                     patch.object(main, "analyze_smart_crop", return_value=dict(crop_plan)), \
                     patch.object(main, "extract_smart_crop_verification_frames", return_value=[b"a", b"b", b"c"]):
                    result_plan = {"scenes": [scene]}
                    if crop_verdict:
                        main.fetch_footage(result_plan)
                        self.assertEqual(scene["smart_vertical_reframe"]["mode"], "ai_content_aware_dynamic_crop")
                        self.assertEqual(scene["output_aspect_ratio"], "9:16")
                        return True
                    with self.assertRaisesRegex(RuntimeError, "could not obtain verified relevant footage"):
                        main.fetch_footage(result_plan)
                    return False
            finally:
                os.chdir(original_cwd)

    def test_landscape_candidate_is_selected_only_after_post_crop_verification(self):
        self.assertTrue(self._run_landscape_candidate(crop_verdict=True))

    def test_landscape_candidate_is_rejected_when_post_crop_verification_fails(self):
        self.assertFalse(self._run_landscape_candidate(crop_verdict=False))


class MM5VisionPayloadTests(unittest.TestCase):
    def test_gemini_crop_tracker_receives_all_six_timestamped_frames(self):
        class FakeResponse:
            status_code = 200
            text = ""
            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": json.dumps({"subject_visible": True})}]}}]}

        captured = {}
        def fake_post(url, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

        with patch.object(main, "post_with_transient_retry", side_effect=fake_post), \
             patch.object(main, "_observe_provider_response", return_value=None), \
             patch.object(main, "VISION_MODEL", "test-vision-model"):
            result = main._verify_with_gemini(
                [b"frame1", b"frame2", b"frame3", b"frame4", b"frame5", b"frame6"],
                "Return crop boxes.", api_key="test-key", return_json=True,
            )
        self.assertTrue(result["subject_visible"])
        parts = captured["json"]["contents"][0]["parts"]
        self.assertEqual(len(parts), 7)  # one instruction + six sampled frames



if __name__ == "__main__":
    unittest.main()
