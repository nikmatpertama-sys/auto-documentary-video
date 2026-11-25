"""Mocked end-to-end orchestration test for the production pipeline.

Every external boundary (AI planning, evidence fetching/audit, Edge TTS, footage
search, FFmpeg) is patched, but the real orchestration, stage reporting, output
validation and production report run. This catches wiring regressions that unit
tests of individual stages cannot see, and it spends no provider quota.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import diagnostics
import main


SCENES = [
    {
        "narration": (
            f"Scene {index} describes how forest moisture moves through canopy air "
            "and returns to the soil below."
        ),
        "visual_focus": "forest canopy",
        "visual_query": "forest canopy light",
        "visual_queries": ["forest canopy rain", "forest canopy mist"],
        "visual_mode": "footage",
        "reference_required": False,
        "reference_query": "",
        "source_label": "",
        "source_url": "",
        "media_file": f"output/footage/scene_{index}.mp4",
        "visual_type": "footage",
        "selected_footage_source": "Pexels",
        "visual_verification": {"method": "vision pixel verification", "confidence": 0.91},
    }
    for index in range(1, 9)
]

FACT_REPORT = {
    "status": "passed",
    "method": "trusted-source fetch + independent evidence-grounded AI audit",
    "claim_count": 8,
    "fact_checker": {"provider": "gemini", "model": "gemini-test"},
    "claims": [
        {
            "id": f"C{index}",
            "scene": index,
            "sentence": scene["narration"],
            "verified_evidence_urls": ["https://www.nasa.gov/scene"],
        }
        for index, scene in enumerate(SCENES, 1)
    ],
    "claim_repairs": [{"claim_id": "C4", "status": "rewritten", "scene": 4}],
}


class MockedPipelineTests(unittest.TestCase):
    def setUp(self):
        # The stage log is process-wide; other tests leave failed stages behind.
        diagnostics.STAGES._stages.clear()

    def test_pipeline_runs_every_stage_and_writes_a_complete_report(self):
        plan = {
            "title": "Forest moisture",
            "hook": "A forest breathes water.",
            "script": " ".join(scene["narration"] for scene in SCENES),
            "scenes": [dict(scene) for scene in SCENES],
            "fact_claims": [
                {
                    "scene": index,
                    "sentence": scene["narration"],
                    "source_urls": ["https://www.nasa.gov/scene"],
                }
                for index, scene in enumerate(SCENES, 1)
            ],
        }

        def fake_footage(pipeline_plan):
            for scene in pipeline_plan["scenes"]:
                path = Path(scene["media_file"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"0" * 512)
            Path("credits.txt").write_text("FOOTAGE CREDITS\nPexels\n", encoding="utf-8")

        def fake_render(_plan, _events, _duration):
            Path("output").mkdir(exist_ok=True)
            Path("documentary.mp4").write_bytes(b"0" * 4096)
            (Path("output") / "voiceover.wav").write_bytes(b"0" * 4096)
            (Path("output") / "voiceover.mp3").write_bytes(b"0" * 4096)
            (Path("output") / "captions.ass").write_text(
                "[Script Info]\n" + "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,A line.\n" * 8,
                encoding="utf-8",
            )
            Path("timing.json").write_text(
                json.dumps(
                    {
                        "caption_groups": [{"text": "line"} for _ in range(12)],
                        "alignment": {"note": "mocked alignment for the orchestration test"},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            Path("voiceover_manifest.json").write_text(
                json.dumps(
                    {
                        "voice": "en-US-GuyNeural",
                        "total_duration": 78.0,
                        "note": "mocked manifest for the orchestration test",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as workdir:
            os.chdir(workdir)
            try:
                with patch.object(main, "TOPIC", "Amazon rainforest moisture"), patch.object(
                    main, "VISION_REQUIRED", True
                ), patch.object(
                    main, "configured_vision_provider_names", return_value=["Gemini (test)"]
                ), patch.object(
                    main, "discover_source_pool", return_value=["https://www.nasa.gov/scene"]
                ), patch.object(
                    main, "gemini_plan", return_value=plan
                ), patch.object(
                    main,
                    "prepare_opening",
                    return_value={"title": "Forest moisture", "hook": "A forest breathes water."},
                ), patch.object(
                    main, "verify_documentary_facts", return_value=FACT_REPORT
                ) as verify, patch.object(
                    main,
                    "build_voiceover",
                    return_value={"sentence_events": [], "total_duration": 78.0},
                ), patch.object(
                    main, "attach_voice_timing_to_scenes"
                ), patch.object(
                    main,
                    "build_subtitles_from_voice",
                    return_value={"alignment": {"caption_group_count": 12}},
                ), patch.object(
                    main, "fetch_footage", side_effect=fake_footage
                ), patch.object(
                    main, "render", side_effect=fake_render
                ), patch.object(
                    main, "ffprobe_duration", return_value=78.0
                ):
                    main._run_pipeline()

                report = json.loads(Path("production_report.json").read_text(encoding="utf-8"))
                fact_written = json.loads(
                    Path("fact_check_report.json").read_text(encoding="utf-8")
                )
                stage_report = json.loads(Path("stage_report.json").read_text(encoding="utf-8"))
                credits_text = Path("credits.txt").read_text(encoding="utf-8")
            finally:
                os.chdir(previous)

        self.assertEqual(report["generation_status"], "complete")
        self.assertEqual(report["scene_count"], 8)
        self.assertTrue(report["all_scenes_visually_verified"])
        self.assertEqual(report["narrative_fact_check"]["status"], "passed")
        self.assertEqual(fact_written["status"], "passed")
        self.assertEqual(verify.call_args.kwargs["repair_claim"] is not None, True)
        stages = [item["stage"] for item in stage_report["run"]["stages"]]
        self.assertIsNone(stage_report["run"]["failed_stage"])
        for stage in (
            "cache_prune",
            "source_pool",
            "script_generation",
            "fact_check",
            "voiceover",
            "timeline_lock",
            "subtitles",
            "footage",
            "render",
            "production_report",
        ):
            self.assertIn(stage, stages)
        self.assertIn("FACT-CHECK SOURCES", credits_text)

    def test_render_failure_is_reported_as_a_failed_run(self):
        plan = {
            "title": "Forest moisture",
            "hook": "A forest breathes water.",
            "script": " ".join(scene["narration"] for scene in SCENES),
            "scenes": [dict(scene) for scene in SCENES],
            "fact_claims": [
                {
                    "scene": index,
                    "sentence": scene["narration"],
                    "source_urls": ["https://www.nasa.gov/scene"],
                }
                for index, scene in enumerate(SCENES, 1)
            ],
        }

        def fake_footage(pipeline_plan):
            for scene in pipeline_plan["scenes"]:
                path = Path(scene["media_file"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"0" * 512)
            Path("credits.txt").write_text("FOOTAGE CREDITS\n", encoding="utf-8")

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as workdir:
            os.chdir(workdir)
            try:
                with patch.object(main, "TOPIC", "Amazon rainforest moisture"), patch.object(
                    main, "VISION_REQUIRED", True
                ), patch.object(
                    main, "configured_vision_provider_names", return_value=["Gemini (test)"]
                ), patch.object(
                    main, "discover_source_pool", return_value=[]
                ), patch.object(
                    main, "gemini_plan", return_value=plan
                ), patch.object(
                    main,
                    "prepare_opening",
                    return_value={"title": "Forest moisture", "hook": "A forest breathes water."},
                ), patch.object(
                    main, "verify_documentary_facts", return_value=FACT_REPORT
                ), patch.object(
                    main,
                    "build_voiceover",
                    return_value={"sentence_events": [], "total_duration": 78.0},
                ), patch.object(
                    main, "attach_voice_timing_to_scenes"
                ), patch.object(
                    main,
                    "build_subtitles_from_voice",
                    return_value={"alignment": {"caption_group_count": 12}},
                ), patch.object(
                    main, "fetch_footage", side_effect=fake_footage
                ), patch.object(
                    main,
                    "render",
                    side_effect=RuntimeError("ffmpeg exited with status 1"),
                ):
                    with self.assertRaises(RuntimeError):
                        main.main()
                failure = json.loads(Path("failure_report.json").read_text(encoding="utf-8"))
                self.assertFalse(Path("production_report.json").exists())
            finally:
                os.chdir(previous)

        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["failed_stage"], "render")
        self.assertIn("ffmpeg", failure["error_message"])


class PreflightFailureTests(unittest.TestCase):
    """A startup configuration failure must name the startup stage."""

    def test_missing_vision_provider_key_is_reported_as_preflight(self):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(main, "TOPIC", "Forest rain"), \
                        patch.object(main, "VISION_REQUIRED", True), \
                        patch.object(
                            main, "configured_vision_provider_names", return_value=[]
                        ):
                    for name in (
                        "GROQ_API_KEY",
                        "DEEPSEEK_API_KEY",
                        "GEMINI_API_KEY",
                        "OPENROUTER_API_KEY",
                    ):
                        os.environ.pop(name, None)
                    with self.assertRaisesRegex(RuntimeError, "vision provider key"):
                        main.main()
                failure = json.loads(
                    Path("failure_report.json").read_text(encoding="utf-8")
                )
                stage_report = json.loads(
                    Path("stage_report.json").read_text(encoding="utf-8")
                )
            finally:
                os.chdir(previous)

        self.assertEqual(failure["failed_stage"], "preflight")
        stages = [item["stage"] for item in stage_report["run"]["stages"]]
        self.assertIn("preflight", stages)
        self.assertEqual(stages[-1], "preflight")


if __name__ == "__main__":
    unittest.main(verbosity=2)
