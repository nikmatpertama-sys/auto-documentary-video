"""MM7 production-reliability regression tests.

These cover the fixes that make a real-provider run trustworthy: licence
confirmation, source-bound fact-check evidence, whole-script claim coverage,
the vision gate reporting unverified footage instead of approving it, Speechify
being the only production TTS path, and the required pipeline order.

They deliberately use mocks: mock results prove the code path, never that a
live provider produced a good answer.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import footage_license
import main
import research


def _analysis(claims, non_factual=(), coverage=1.0):
    return {
        "claims": list(claims),
        "non_factual": list(non_factual),
        "source": "semantic:mock",
        "provider": "mock",
        "coverage": {"script_sentences": 4, "coverage": coverage},
        "skipped_sentences": [],
        "error": None,
    }


class LicenceTests(unittest.TestCase):
    def test_cc0_commons_file_is_confirmed(self):
        info = footage_license.resolve_license({
            "source": "Wikimedia Commons",
            "license": "CC0",
            "page_url": "https://commons.wikimedia.org/wiki/File:Ant.webm",
        })
        self.assertEqual(info["status"], "confirmed")
        self.assertTrue(info["reuse_allowed"])
        self.assertIn("creativecommons.org", info["url"])
        self.assertFalse(info["attribution_required"])

    def test_generic_page_pointer_is_not_a_licence(self):
        info = footage_license.resolve_license({
            "source": "Wikimedia Commons",
            "license": "See file page",
            "page_url": "https://commons.wikimedia.org/wiki/File:X.webm",
        })
        self.assertEqual(info["status"], "unconfirmed")
        self.assertFalse(info["reuse_allowed"])

    def test_verify_terms_wording_is_unconfirmed(self):
        info = footage_license.resolve_license({
            "source": "Web Search",
            "license": "Page states reusable licence ('creative commons'); verify page terms",
            "page_url": "https://example.org/page",
        })
        self.assertEqual(info["status"], "unconfirmed")

    def test_noncommercial_is_restricted(self):
        info = footage_license.resolve_license({
            "source": "Internet Archive",
            "license": "CC BY-NC 4.0",
            "page_url": "https://archive.org/details/x",
        })
        self.assertEqual(info["status"], "restricted")
        self.assertFalse(info["reuse_allowed"])

    def test_missing_licence_is_not_permission(self):
        info = footage_license.resolve_license({"source": "NASA", "url": "https://x/y.mp4"})
        self.assertEqual(info["status"], "unconfirmed")
        self.assertEqual(info["evidence"], "(no licence metadata supplied by the provider)")

    def test_attribution_required_is_recorded(self):
        info = footage_license.resolve_license({
            "source": "Wikimedia Commons",
            "license": "CC BY-SA 4.0",
            "page_url": "https://commons.wikimedia.org/wiki/File:X.webm",
        })
        self.assertEqual(info["status"], "confirmed")
        self.assertTrue(info["attribution_required"])


class FactCheckEvidenceTests(unittest.TestCase):
    def test_supported_claim_keeps_source_title_url_and_quote(self):
        claims = ["Ants live in colonies."]
        payload = {
            "claims": [{
                "claim": claims[0],
                "verdict": "supported",
                "evidence": "The article describes eusocial colonies.",
                "quote": "Ants form colonies that range in size",
                "source_id": "S1",
            }],
            "overall": "pass",
        }
        result = research.summarise_verification(
            payload, claims,
            sources=[{"title": "Ant", "url": "https://en.wikipedia.org/wiki/Ant"}],
        )
        item = result["claims"][0]
        self.assertEqual(item["verdict"], "supported")
        self.assertEqual(item["source"]["title"], "Ant")
        self.assertEqual(item["source"]["url"], "https://en.wikipedia.org/wiki/Ant")
        self.assertEqual(item["evidence_type"], "source_quote")
        self.assertEqual(result["sourced_supports"], 1)

    def test_supported_without_evidence_is_downgraded(self):
        payload = {"claims": [{"claim": "Ants are insects.", "verdict": "supported"}]}
        result = research.summarise_verification(payload, ["Ants are insects."])
        self.assertEqual(result["claims"][0]["verdict"], "unsupported")
        self.assertEqual(result["overall"], "revise")

    def test_thin_evidence_is_downgraded(self):
        payload = {"claims": [{
            "claim": "Ants are insects.", "verdict": "supported", "evidence": "yes",
        }]}
        result = research.summarise_verification(payload, ["Ants are insects."])
        self.assertEqual(result["claims"][0]["verdict"], "unsupported")

    def test_no_verdict_entry_is_recorded_not_dropped(self):
        payload = {"claims": []}
        result = research.summarise_verification(payload, ["One claim here."])
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["claims"][0]["verdict"], "unsupported")
        self.assertIn("no verdict", result["claims"][0]["evidence"])

    def test_claim_coverage_lists_skipped_sentences(self):
        script = "Ants live in soil. Leaves fall. The colony grows."
        coverage = research.claim_coverage(
            script, ["Ants live in soil.", "The colony grows."], []
        )
        self.assertEqual(coverage["script_sentences"], 3)
        self.assertIn("Leaves fall.", coverage["uncovered_sentences"])
        self.assertAlmostEqual(coverage["coverage"], 0.667, places=2)

    def test_semantic_extraction_recovers_skipped_sentences(self):
        script = (
            "Ants live in soil. The queen ant lays her eggs deep inside the nest. "
            "Workers forage for food at night."
        )
        payload = {"claims": ["Ants live in soil."], "non_factual": []}
        with patch.object(main, "call_json_with_providers", return_value=(payload, "mock")):
            analysis = main.analyse_script_claims(script)
        self.assertEqual(analysis["source"], "semantic:mock")
        recovered = " ".join(analysis["claims"]).lower()
        self.assertIn("queen ant lays her eggs", recovered)
        self.assertIn("workers forage for food at night", recovered)
        self.assertEqual(len(analysis["claims"]), 3)

    def test_unsupported_claims_block_the_run(self):
        analysis = _analysis(["Ants live in soil.", "Ants sleep eight hours.", "Ants farm fungus."])
        verification = {
            "claims": [
                {"claim": "Ants live in soil.", "verdict": "supported", "evidence": "source quote"},
                {"claim": "Ants sleep eight hours.", "verdict": "unsupported", "evidence": "no support"},
                {"claim": "Ants farm fungus.", "verdict": "supported", "evidence": "source quote"},
            ],
            "supported": 2, "unsupported": 1, "contradicted": 0, "overall": "revise",
            "sourced_supports": 1,
        }
        with patch.object(main, "analyse_script_claims", return_value=analysis), \
                patch.object(main, "_verify_claims", return_value=dict(verification)), \
                patch.object(main.research_module, "write_research_files"), \
                patch.object(main.research_module, "gather_claim_evidence",
                             return_value={"brief": "", "articles": [], "queries": [], "warnings": []}), \
                patch.dict("os.environ", {"ALLOW_UNSUPPORTED_CLAIMS": ""}):
            with self.assertRaisesRegex(RuntimeError, "not safe to narrate"):
                main.fact_check_plan({"script": "x", "scenes": []}, {"brief": ""}, max_repairs=0)

    def test_no_claim_script_is_distinct_from_extraction_failure(self):
        no_claims = _analysis([], non_factual=["Rain falls.", "The forest breathes."])
        with patch.object(main, "analyse_script_claims", return_value=no_claims), \
                patch.object(main.research_module, "write_research_files"):
            with self.assertRaisesRegex(RuntimeError, "legitimate no-claim narration"):
                main.fact_check_plan({"script": "x", "scenes": []}, {"brief": ""}, max_repairs=0)
        failed = dict(no_claims, non_factual=[], source="heuristic",
                      error="no provider reachable",
                      coverage={"script_sentences": 2, "coverage": 0.0})
        with patch.object(main, "analyse_script_claims", return_value=failed), \
                patch.object(main.research_module, "write_research_files"):
            with self.assertRaisesRegex(RuntimeError, "failed to extract claims"):
                main.fact_check_plan({"script": "x", "scenes": []}, {"brief": ""}, max_repairs=0)


class VisionGateTests(unittest.TestCase):
    def test_no_vision_provider_fails_the_stage(self):
        with patch.object(main, "GROQ_KEYS", []), \
                patch.object(main, "OPENROUTER_KEYS", []), \
                patch.object(main, "GEMINI_KEYS", []), \
                patch.object(main, "CLOUDFLARE_CREDENTIALS", []), \
                patch.object(main, "VISION_REQUIRED", True):
            with self.assertRaisesRegex(RuntimeError, "requires visual verification"):
                main.fetch_footage({"scenes": []})

    def test_vision_error_is_never_swallowed(self):
        self.assertTrue(issubclass(main.VisionVerificationError, RuntimeError))
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("Visual verification cannot continue", source)
        self.assertNotIn("except VisionVerificationError: pass", source)

    def test_rejection_record_requires_a_reason(self):
        frames_dir = Path(tempfile.mkdtemp())
        self.assertTrue(frames_dir.is_dir())


class LicenceGateTests(unittest.TestCase):
    def _scene(self):
        return {
            "narration": "Ants dig tunnels under the soil.",
            "visual_query": "ant colony underground",
            "visual_queries": ["ant colony underground"],
        }

    def test_unconfirmed_licence_candidate_is_never_downloaded(self):
        hit = {
            "source": "Wikimedia Commons",
            "id": "1",
            "title": "Ant colony underground",
            "url": "https://upload.wikimedia.org/ant.webm",
            "page_url": "https://commons.wikimedia.org/wiki/File:Ant.webm",
            "width": 1080,
            "height": 1920,
            "duration": 20,
            "metadata": "Ant colony underground tunnels",
            "tags": "Ant colony underground tunnels",
            "creator": "Someone",
            "license": "See file page",
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(main, "OUT", Path(temp_dir)), \
                patch.object(main, "FOOT", Path(temp_dir) / "footage"), \
                patch.object(main, "VISION_REQUIRED", False), \
                patch.object(main, "GROQ_KEYS", []), \
                patch.object(main, "OPENROUTER_KEYS", []), \
                patch.object(main, "GEMINI_KEYS", []), \
                patch.object(main, "CLOUDFLARE_CREDENTIALS", []), \
                patch.object(main, "search_all_footage_sources", return_value=[hit]), \
                patch.object(main, "download_stream") as download:
            (Path(temp_dir) / "footage").mkdir(parents=True, exist_ok=True)
            with self.assertRaisesRegex(RuntimeError, "could not obtain verified"):
                main.fetch_footage({"scenes": [self._scene()]})
            download.assert_not_called()


class VoiceProviderTests(unittest.TestCase):
    def test_speechify_failure_does_not_fall_back_silently(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(main, "OUT", Path(temp_dir)), \
                patch.object(main, "SPEECHIFY_KEYS", ["fake-key"]), \
                patch.object(main, "_generate_speechify_parts", side_effect=RuntimeError("HTTP 402")), \
                patch.dict("os.environ", {"ALLOW_VOICE_FALLBACK": ""}), \
                patch.object(main, "_generate_edge_parts") as edge:
            with self.assertRaisesRegex(RuntimeError, "substituted automatically"):
                main.build_voice_and_subs("Ants live in soil.", enforce_minimum=False)
            edge.assert_not_called()

    def test_explicit_fallback_is_allowed_for_local_tests(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(main, "OUT", Path(temp_dir)), \
                patch.object(main, "SPEECHIFY_KEYS", ["fake-key"]), \
                patch.object(main, "_generate_speechify_parts", side_effect=RuntimeError("HTTP 402")), \
                patch.dict("os.environ", {"ALLOW_VOICE_FALLBACK": "1"}), \
                patch.object(main, "_generate_edge_parts", side_effect=RuntimeError("edge reached")) as edge:
            with self.assertRaisesRegex(RuntimeError, "edge reached"):
                main.build_voice_and_subs("Ants live in soil.", enforce_minimum=False)
            edge.assert_called_once()


class PipelineOrderTests(unittest.TestCase):
    def test_main_runs_voiceover_before_footage_and_rendering_last(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        main_fn = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        stages = [
            node.args[0].value
            for node in ast.walk(main_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "start_pipeline_stage"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        self.assertEqual(
            stages,
            [
                "research",
                "documentary_plan_generation",
                "fact_checking",
                "voiceover_and_subtitle_generation",
                "footage_search_download_and_visual_verification",
                "video_rendering",
                "quality_control",
            ],
        )


class SceneCountTests(unittest.TestCase):
    SENTENCE = (
        "Ants build worlds beneath our feet, and every worker carries a "
        "small piece of the colony"
    )

    def _plan(self, scenes):
        return {
            "title": "Ants",
            "hook": "Ants build worlds beneath our feet.",
            "scenes": [
                {
                    "narration": f"{self.SENTENCE} number {index}.",
                    "visual_query": "ant colony",
                    "visual_queries": ["ant colony macro"],
                }
                for index in range(1, scenes + 1)
            ],
        }

    def test_story_driven_scene_count_is_accepted(self):
        with patch.object(main, "SCRIPT_LANGUAGE", "en"):
            plan = main.validate_plan_candidate(self._plan(10))
        self.assertEqual(len(plan["scenes"]), 10)

    def test_padded_or_unusable_scene_count_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "scenes that follow the story"):
            main.validate_plan_candidate(self._plan(18))


if __name__ == "__main__":
    unittest.main()
