"""MM6 tests: language, captions, research, fact-check, music and QC."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import qc
import reference_style
import research

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")

INDONESIAN_SCRIPT = (
    "Komodo adalah kadal terbesar yang masih hidup di bumi, dan hanya ditemukan "
    "di beberapa pulau kering Indonesia bagian tengah. "
    "Panjang tubuh komodo dewasa bisa melebihi dua meter, dengan berat yang "
    "mencapai puluhan kilogram pada jantan besar. "
    "Saat berburu, komodo mengandalkan lidah bercabang untuk mencium bau bangkai "
    "dari jarak yang jauh. "
    "Gigi bergerigi dan rahang yang kuat membantunya mencabik daging buruan yang "
    "berukuran besar. "
    "Anak komodo memanjat pohon untuk menghindari komodo dewasa, yang bisa "
    "memangsa anak kecil tanpa ragu. "
    "Suhu sarang tempat telur dikubur menentukan jenis kelamin anak yang menetas "
    "beberapa bulan kemudian. "
    "Komodo muda menghabiskan banyak waktu di atas pohon sampai tubuhnya cukup "
    "besar dan kuat. "
    "Kini populasinya dijaga agar tetap bertahan di alam liar, dan wisatawan "
    "yang datang diarahkan menjaga jarak aman dari hewan itu."
)

ENGLISH_SCRIPT = (
    "The komodo dragon is the largest living lizard on earth, and it is found "
    "only on a few dry Indonesian islands. "
    "An adult komodo can grow longer than two metres and weigh dozens of "
    "kilograms. "
    "When hunting, it relies on a forked tongue to smell carrion from a long "
    "distance away. "
    "Serrated teeth and a powerful jaw help it tear the flesh of large prey "
    "apart. "
    "Young komodos climb trees to avoid adult komodos, which can prey on small "
    "hatchlings. "
    "The temperature of the nest where eggs are buried determines the sex of "
    "the hatchlings. "
    "Juvenile komodos spend much of their time above ground until their bodies "
    "are large enough. "
    "Its population is now protected in the wild, and visitors are asked to "
    "keep a safe distance."
)


def make_plan(script=ENGLISH_SCRIPT):
    sentences = [s.strip() for s in script.split(".") if s.strip()]
    # One sentence per scene, padding by splitting if needed.
    while len(sentences) < 8:
        sentences.append("Fakta ini penting untuk dipahami dan dicatat baik-baik")
    scenes = []
    for index in range(8):
        scenes.append({
            "narration": sentences[index] + ".",
            "visual_query": "komodo dragon walking",
            "visual_queries": ["komodo dragon close up", "komodo dragon habitat"],
            "source_label": "",
            "source_url": "",
        })
    return {
        "title": "Komodo",
        "hook": "Kadal raksasa ini hanya hidup di Indonesia.",
        "script": " ".join(scene["narration"] for scene in scenes),
        "scenes": scenes,
    }


class LanguageProfileTests(unittest.TestCase):
    def test_default_language_is_english(self):
        # The project's narration and VO foundation is English; Indonesian stays
        # available through SCRIPT_LANGUAGE=id.
        self.assertEqual(reference_style.resolve_language_code(""), "en")
        self.assertEqual(reference_style.resolve_language_code("en-US"), "en")
        self.assertEqual(reference_style.resolve_language_code("id-ID"), "id")
        self.assertEqual(reference_style.resolve_language_code("in_ID"), "id")

    def test_profiles_use_matching_free_tts_voices(self):
        self.assertEqual(
            reference_style.language_profile("id")["edge_voice"], "id-ID-ArdiNeural"
        )
        self.assertEqual(
            reference_style.language_profile("en")["edge_voice"], "en-US-GuyNeural"
        )

    def test_language_mismatch_detection(self):
        self.assertTrue(reference_style.narration_language_matches(INDONESIAN_SCRIPT, "id"))
        self.assertFalse(reference_style.narration_language_matches(ENGLISH_SCRIPT, "id"))
        self.assertTrue(reference_style.narration_language_matches(ENGLISH_SCRIPT, "en"))

    def test_language_check_is_lenient_for_short_text(self):
        self.assertTrue(reference_style.narration_language_matches("Komodo.", "id"))


class CaptionStyleTests(unittest.TestCase):
    def test_reference_caption_block_is_vertical_and_above_ui(self):
        style = reference_style.caption_style("id")
        self.assertTrue(style["uppercase"])
        self.assertLessEqual(style["max_words"], 4)
        self.assertGreaterEqual(style["margin_v"], 400)
        header = "\n".join(reference_style.caption_ass_header(style))
        self.assertIn("PlayResX: 1080", header)
        self.assertIn("PlayResY: 1920", header)
        self.assertIn(",70,70,420,1", header)
        self.assertIn(",-1,0,0,0,", header)  # bold flag

    def test_caption_text_uppercases_indonesian(self):
        self.assertEqual(
            reference_style.format_caption_text("komodo  dewasa", uppercase=True),
            "KOMODO DEWASA",
        )
        self.assertEqual(
            reference_style.format_caption_text("Komodo dewasa", uppercase=False),
            "Komodo dewasa",
        )


class ResearchTests(unittest.TestCase):
    def test_extract_claims_finds_numbers_and_absolutes(self):
        claims = research.extract_claims(INDONESIAN_SCRIPT)
        self.assertTrue(any("dua meter" in claim for claim in claims))
        self.assertTrue(all(len(claim.split()) >= 4 for claim in claims))

    def test_prompt_contains_source_material_and_language_rules(self):
        prompt = research.fact_check_prompt(
            INDONESIAN_SCRIPT, ["Komodo bisa melebihi dua meter."], "Komodo panjang 2-3 m."
        )
        self.assertIn("Komodo panjang 2-3 m.", prompt)
        self.assertIn("verdict", prompt)

    def test_verification_summary_flags_contradictions(self):
        payload = {
            "claims": [
                {"claim": "A", "verdict": "supported", "evidence": "matches"},
                {"claim": "B", "verdict": "contradicted", "evidence": "conflict"},
            ]
        }
        result = research.summarise_verification(payload, ["A", "B"])
        self.assertEqual(result["overall"], "fail")
        self.assertEqual(result["contradicted"], 1)

    def test_verification_summary_missing_entries_are_unsupported(self):
        result = research.summarise_verification({"claims": []}, ["only claim"])
        self.assertEqual(result["overall"], "revise")
        self.assertEqual(result["unsupported"], 1)

    def test_brief_is_bounded_and_mentions_sources(self):
        articles = [
            {"language": "id", "title": "Komodo", "url": "https://id.wikipedia.org/wiki/Komodo",
             "extract": "Komodo adalah kadal." * 500},
            {"language": "en", "title": "Komodo dragon",
             "url": "https://en.wikipedia.org/wiki/Komodo_dragon", "extract": "Lizard."},
        ]
        brief = research.build_research_brief(articles, budget=1200)
        self.assertLessEqual(len(brief), 1200)
        self.assertIn("id.wikipedia.org", brief)


class MusicAndSfxTests(unittest.TestCase):
    def test_music_expression_builds_a_chord(self):
        expression = reference_style.music_bed_expression(0)
        self.assertIn("sin(", expression)
        self.assertIn("110.0", expression)
        self.assertNotEqual(expression, reference_style.music_bed_expression(1))

    def test_music_filter_fades_in_and_out(self):
        chain = reference_style.music_bed_filter(80)
        self.assertIn("afade=t=in", chain)
        self.assertIn("afade=t=out", chain)

    @unittest.skipUnless(FFMPEG, "FFmpeg is required")
    def test_music_bed_renders_expected_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "music.wav"
            reference_style.build_music_bed(6.0, output)
            self.assertTrue(output.is_file())
            duration = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(output)],
                capture_output=True, text=True, check=True,
            ).stdout.strip())
            self.assertAlmostEqual(duration, 6.0, delta=0.5)

    @unittest.skipUnless(FFMPEG, "FFmpeg is required")
    def test_whoosh_and_impact_are_generated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            whoosh = reference_style.build_whoosh(Path(temp_dir) / "whoosh.wav")
            impact = reference_style.build_impact(Path(temp_dir) / "impact.wav")
            self.assertGreater(whoosh.stat().st_size, 1000)
            self.assertGreater(impact.stat().st_size, 1000)


class QualityControlTests(unittest.TestCase):
    @unittest.skipUnless(FFMPEG, "FFmpeg is required")
    def test_probe_media_reads_vertical_dimensions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clip = Path(temp_dir) / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=size=360x640:rate=12",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                 "-t", "1.2", "-shortest", "-pix_fmt", "yuv420p", str(clip)],
                check=True,
            )
            media = qc.probe_media(clip)
            self.assertEqual((media["width"], media["height"]), (360, 640))
            self.assertTrue(media["has_audio"])
            self.assertAlmostEqual(media["duration"], 1.2, delta=0.3)

    @unittest.skipUnless(FFMPEG, "FFmpeg is required")
    def test_black_frame_detection_finds_a_black_clip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            clip = Path(temp_dir) / "black.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=black:size=360x640:rate=12",
                 "-t", "1.5", "-pix_fmt", "yuv420p", str(clip)],
                check=True,
            )
            blacks = qc.detect_black_frames(clip, min_duration=0.5)
            self.assertTrue(blacks)
            self.assertGreaterEqual(blacks[0]["duration"], 0.5)

    def test_caption_event_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "captions.ass"
            path.write_text(
                "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,HALO\n",
                encoding="utf-8",
            )
            self.assertEqual(qc.count_caption_events(path), 1)


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        import main as main_module
        self.main = main_module

    def test_valid_english_plan_is_accepted_and_mapped(self):
        plan = self.main.validate_plan_candidate(make_plan())
        self.assertEqual(len(plan["scenes"]), 8)
        for scene in plan["scenes"]:
            self.assertIn("sentence_start", scene)

    def test_valid_indonesian_plan_is_accepted_when_configured(self):
        from unittest.mock import patch

        with patch.object(self.main, "SCRIPT_LANGUAGE", "id"):
            plan = self.main.validate_plan_candidate(make_plan(INDONESIAN_SCRIPT))
        self.assertEqual(len(plan["scenes"]), 8)
        for scene in plan["scenes"]:
            self.assertIn("sentence_start", scene)

    def test_indonesian_plan_is_rejected_for_english_runs(self):
        if self.main.SCRIPT_LANGUAGE != "en":
            self.skipTest("project default language is not English")
        with self.assertRaisesRegex(RuntimeError, "not written in"):
            self.main.validate_plan_candidate(make_plan(INDONESIAN_SCRIPT))

    def test_plan_without_a_word_minimum_is_accepted(self):
        # MM7.5: there is no word minimum and no word maximum. Only the spoken
        # voice-over duration decides whether the narration is long enough.
        plan = make_plan()
        short = [
            "Komodo dragons hunt deer.",
            "They ambush prey on open ground.",
            "A flicking tongue samples the air.",
            "Females guard nests in the soil.",
            "Hatchlings climb trees to survive.",
            "Adults patrol dry riverbeds.",
            "Their bite delivers venom.",
            "Few predators challenge them.",
        ]
        for scene, sentence in zip(plan["scenes"], short):
            scene["narration"] = sentence
        plan["script"] = " ".join(short)
        validated = self.main.validate_plan_candidate(plan)
        self.assertEqual(len(validated["scenes"]), 8)
        self.assertLess(len(validated["script"].split()), 120)
        self.assertTrue(self.main.script_cta.ends_with_cta(validated["script"]))

    def test_duration_extension_keeps_approved_footage(self):
        from unittest.mock import patch

        plan = self.main.validate_plan_candidate(make_plan())
        for index, scene in enumerate(plan["scenes"]):
            scene["media_file"] = f"output/footage/scene_{index}.mp4"
            scene["license"] = "CC BY-SA 4.0"
            scene["selected_footage_source"] = "Wikimedia Commons"

        expanded = make_plan()
        # Deepened wording; no word limit applies to the extension.
        for scene in expanded["scenes"]:
            scene["narration"] = scene["narration"].rstrip(".") + ", dan itu penting."
        expanded["script"] = " ".join(
            scene["narration"] for scene in expanded["scenes"]
        )

        with patch.object(
            self.main, "call_json_with_providers", return_value=(expanded, "Mock")
        ):
            result = self.main.extend_plan_for_duration(plan, "brief")

        for index, scene in enumerate(result["scenes"]):
            self.assertEqual(scene["media_file"], f"output/footage/scene_{index}.mp4")
            self.assertEqual(scene["license"], "CC BY-SA 4.0")
            self.assertEqual(scene["selected_footage_source"], "Wikimedia Commons")
            self.assertTrue(scene["visual_query"])
            self.assertIn("sentence_start", scene)

    def test_long_hook_is_trimmed_to_twelve_words(self):
        plan = make_plan()
        plan["hook"] = " ".join(["kata"] * 20)
        validated = self.main.validate_plan_candidate(plan)
        self.assertLessEqual(len(validated["hook"].split()), 12)


class CopyrightReportTests(unittest.TestCase):
    def test_report_lists_every_scene_and_music_provenance(self):
        plan = make_plan()
        for index, scene in enumerate(plan["scenes"], 1):
            scene.update({
                "source": "Wikimedia Commons",
                "creator": "Tester",
                "page_url": f"https://commons.wikimedia.org/wiki/File:test{index}.webm",
                "license": "CC BY-SA 4.0",
            })
        report = reference_style.build_copyright_report(
            plan, "id", "Edge TTS", reference_style.MUSIC_BED_PROVENANCE
        )
        self.assertEqual(len(report["footage"]), 8)
        self.assertTrue(report["music"]["enabled"])
        self.assertIn("CC BY-SA 4.0", json.dumps(report))
        self.assertEqual(report["video_format"]["aspect_ratio"], "9:16")


if __name__ == "__main__":
    unittest.main()
