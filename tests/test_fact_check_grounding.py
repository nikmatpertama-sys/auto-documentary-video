"""Regression tests for evidence grounding and free-provider routing.

These cover the failure modes seen in archived runs: a page that opened but was
about another subject, numeric claims without the number, 403/404 hosts, PMC
full-text extraction, and a planner that had no way to replace an unverifiable
claim. Everything is mocked unless the test explicitly labels it as network-free
discovery; no provider quota is spent.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_router
import fact_check
from fact_check import (
    FactCheckError,
    claim_evidence_gate,
    claim_number_requirements,
    extract_page_text,
    number_requirement_present,
    verify_documentary_facts,
)


SCENES = [
    "The Amazon basin contains extensive tropical rainforest habitat that spans nine countries and covers millions of hectares of land.",
    "Seasonal rainfall patterns shape the growth, flowering, and fruiting cycles of forest vegetation throughout the year in a predictable rhythm.",
    "Trees release large amounts of moisture into the atmosphere through a process that scientists call transpiration during daylight hours.",
    "Air moving across the forest carries that moisture toward distant inland regions and influences local weather and rainfall downwind of the trees.",
    "Scientists measure rainfall isotopes to trace where the water vapor originated before it fell on the forest canopy and soaked into the soil.",
    "Forest loss can reduce regional rainfall and raise surface temperatures across cleared land nearby and downwind of the cleared area.",
    "Indigenous territories protect large areas of intact forest and maintain biodiversity across the wider landscape over many generations.",
    "River systems carry sediment and nutrients from distant headwaters toward the Atlantic coast each year during the wet season floods.",
]


def build_plan(sentence_overrides=None):
    sentences = [dict(sentence_overrides or {}).get(index, text) for index, text in enumerate(SCENES, 1)]
    claims = [
        {
            "scene": index,
            "sentence": sentence,
            "source_urls": [f"https://www.nasa.gov/scene-{index}"],
        }
        for index, sentence in enumerate(sentences, 1)
    ]
    return {
        "topic": "Amazon rainforest moisture recycling",
        "script": " ".join(sentences),
        "scenes": [{"narration": sentence} for sentence in sentences],
        "fact_claims": claims,
    }


def page(url, text, title="Trusted source page", status=200):
    return {
        "requested_url": url,
        "url": url,
        "host": url.split("//", 1)[1].split("/", 1)[0],
        "status_code": status,
        "content_type": "text/html",
        "title": title,
        "text": text,
    }


def pages_for_plan(plan, override=None):
    mapping = {}
    for index, scene in enumerate(plan["scenes"], 1):
        url = f"https://www.nasa.gov/scene-{index}"
        body = (scene["narration"] + " ") * 3
        mapping[url] = page(url, body, f"Amazon research scene {index}")
    mapping.update(override or {})
    return mapping


def passing_checker(stage_marker=None):
    def checker(prompt, validator=None, stage=None):
        if stage_marker is not None:
            stage_marker.append(stage)
        payload = json.loads(
            prompt.split("CLAIMS AND FETCHED EVIDENCE:", 1)[1]
            .split("\n\nFor each supplied claim", 1)[0]
        )
        verdicts = [
            {
                "claim_id": item["claim_id"],
                "status": "supported",
                "evidence_urls": [item["sources"][0]["url"]],
                "reason": "The fetched excerpt directly supports the sentence.",
            }
            for item in payload
        ]
        return (
            validator(
                {
                    "overall_status": "pass",
                    "claim_verdicts": verdicts,
                    "uncited_factual_sentences": [],
                    "coverage_note": "Every sentence is covered by fetched evidence.",
                }
            ),
            "gemini",
            "gemini-test",
        )

    return checker


class EvidenceGroundingTests(unittest.TestCase):
    def setUp(self):
        fact_check._HOST_UNAVAILABLE.clear()
        fact_check._DISCOVERY_CACHE.clear()
        fact_check._SCHOLARLY_CACHE.clear()

    def test_claim_gate_rejects_a_page_about_another_subject(self):
        with self.assertRaisesRegex(FactCheckError, "different subject"):
            claim_evidence_gate(
                "A page about spacecraft engineering and orbital observations. " * 6,
                "The Amazon basin contains extensive tropical rainforest habitat.",
            )

    def test_fetched_but_unrelated_page_is_not_evidence(self):
        plan = build_plan()
        pages = pages_for_plan(
            plan,
            {
                "https://www.nasa.gov/scene-1": page(
                    "https://www.nasa.gov/scene-1",
                    "A page about spacecraft engineering and orbital observations. " * 8,
                    "Unrelated NASA page",
                )
            },
        )
        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: pages[url]):
            with patch.object(fact_check, "discover_trusted_source_urls", return_value=[]):
                with patch.object(fact_check, "discover_scholarly_source_urls", return_value=[]):
                    with self.assertRaises(FactCheckError) as context:
                        verify_documentary_facts(plan, lambda *_a, **_k: self.fail("audit must not run"))

        report = context.exception.report
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["blocked_at_claim"], "C1")
        unusable = [
            item for item in report["sources"]
            if item.get("requested_url") == "https://www.nasa.gov/scene-1"
        ][0]
        self.assertEqual(unusable["status"], "not_usable")
        self.assertIn("different subject", unusable["reason"])

    def test_numeric_claim_needs_the_claimed_value_in_the_page(self):
        sentence = "During the wet season, up to 75% of regional rainfall comes from forest moisture."
        plan = build_plan({1: sentence})
        plan["fact_claims"][0]["sentence"] = sentence
        plan["scenes"][0]["narration"] = sentence
        plan["script"] = " ".join(scene["narration"] for scene in plan["scenes"])
        pages = pages_for_plan(
            plan,
            {
                "https://www.nasa.gov/scene-1": page(
                    "https://www.nasa.gov/scene-1",
                    (
                        "Regional rainfall comes from forest moisture during the wet season. "
                        "The study reports median and 75th percentile values for rainfall. "
                    ) * 6,
                    "Rainfall percentile study",
                )
            },
        )
        self.assertEqual(
            claim_number_requirements(sentence),
            [{"value": "75", "unit": "percent"}],
        )
        self.assertFalse(
            number_requirement_present(
                "median, 25th and 75th percentiles", {"value": "75", "unit": "percent"}
            )
        )
        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: pages[url]):
            with patch.object(fact_check, "discover_trusted_source_urls", return_value=[]):
                with patch.object(fact_check, "discover_scholarly_source_urls", return_value=[]):
                    with self.assertRaises(FactCheckError) as context:
                        verify_documentary_facts(plan, lambda *_a, **_k: self.fail("audit must not run"))
        self.assertIn("does not state the claimed value", str(context.exception))

    def test_host_that_returns_403_is_not_requested_again(self):
        plan = build_plan()
        plan["fact_claims"][0]["source_urls"] = [
            "https://www.britannica.com/page-one",
            "https://www.britannica.com/page-two",
        ]
        calls = []

        def fake_fetch(url):
            calls.append(url)
            raise FactCheckError(
                f"Could not fetch evidence source {url}: HTTPError (HTTP 403)"
            )

        with patch.object(fact_check, "fetch_source_page", side_effect=fake_fetch):
            with patch.object(fact_check, "discover_trusted_source_urls", return_value=[]):
                with patch.object(fact_check, "discover_scholarly_source_urls", return_value=[]):
                    with self.assertRaises(FactCheckError) as context:
                        verify_documentary_facts(plan, lambda *_a, **_k: self.fail("audit must not run"))

        self.assertEqual(calls, ["https://www.britannica.com/page-one"])
        reasons = " ".join(
            item.get("reason", "") for item in context.exception.report["sources"]
        )
        self.assertIn("already blocked in this run", reasons)

    def test_metadata_abstract_is_used_when_the_body_is_script_rendered(self):
        document = (
            "<html><head><title>Forest moisture study</title>"
            '<meta name="citation_abstract" content="Forest moisture recycling supplies a large '
            "share of regional rainfall during the wet season, according to isotope measurements "
            "collected across the basin over five years. The authors combined precipitation "
            "sampling, canopy flux towers and atmospheric transport modelling to separate "
            "forest-derived moisture from oceanic sources across the study region.\"></head>"
            "<body></body></html>"
        ).encode("utf-8")
        title, text = extract_page_text(document, "text/html")
        self.assertEqual(title, "Forest moisture study")
        self.assertIn("isotope measurements", text)

    def test_jats_xml_full_text_is_extracted_without_reference_list(self):
        xml = (
            "<?xml version=\"1.0\"?><article><front><article-meta><title-group>"
            "<article-title>Rainfall recycling in tropical forests</article-title></title-group>"
            "<abstract><p>Forest transpiration returns moisture to the atmosphere and sustains "
            "regional rainfall recycling across the basin during the wet season.</p></abstract>"
            "</article-meta></front><body><p>Measurements from the basin show that forest "
            "transpiration contributes a substantial share of atmospheric moisture.</p></body>"
            "<back><ref-list><ref><mixed-citation>Smith 2020, a reference that should be "
            "dropped from the evidence text.</mixed-citation></ref></ref-list></back></article>"
        ).encode("utf-8")
        title, text = extract_page_text(xml, "text/xml; charset=UTF-8")
        self.assertEqual(title, "Rainfall recycling in tropical forests")
        self.assertIn("transpiration returns moisture", text)
        self.assertNotIn("should be dropped", text)

    def test_anti_bot_challenge_page_is_not_treated_as_evidence(self):
        challenge = (
            "<html><head><base href='https://www.google.com/recaptcha/challengepage/'></head>"
            "<body><p>Please complete the recaptcha challenge to continue.</p></body></html>"
        ).encode("utf-8")
        with self.assertRaisesRegex(FactCheckError, "anti-bot challenge"):
            extract_page_text(challenge, "text/html")


class ClaimRepairTests(unittest.TestCase):
    def setUp(self):
        fact_check._HOST_UNAVAILABLE.clear()
        fact_check._DISCOVERY_CACHE.clear()
        fact_check._SCHOLARLY_CACHE.clear()

    def test_unsupported_claim_is_rewritten_from_available_evidence(self):
        plan = build_plan()
        self.assertGreaterEqual(len(plan["script"].split()), 150)
        replacement = (
            "The Amazon basin contains extensive tropical rainforest habitat that spans "
            "nine countries and covers millions of hectares of forest."
        )
        pages = pages_for_plan(
            plan,
            {
                "https://www.nasa.gov/scene-1": page(
                    "https://www.nasa.gov/scene-1",
                    "A page about spacecraft engineering and orbital observations. " * 8,
                    "Unrelated NASA page",
                ),
                "https://www.fao.org/amazon-habitat": page(
                    "https://www.fao.org/amazon-habitat",
                    (replacement + " ") * 3,
                    "FAO Amazon habitat assessment",
                ),
            },
        )
        repairs = []

        def repair(claim, context, current_plan):
            repairs.append((claim["id"], [item["url"] for item in context["evidence"]]))
            return {"sentence": replacement, "source_urls": ["https://www.fao.org/amazon-habitat"]}

        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: pages[url]):
            with patch.object(fact_check, "discover_trusted_source_urls", return_value=[]):
                with patch.object(fact_check, "discover_scholarly_source_urls", return_value=[]):
                    report = verify_documentary_facts(
                        plan, passing_checker(), repair_claim=repair
                    )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["claim_repairs"]), 1)
        self.assertEqual(report["claim_repairs"][0]["status"], "rewritten")
        self.assertEqual(plan["scenes"][0]["narration"], replacement)
        self.assertIn(replacement, plan["script"])
        self.assertEqual(repairs[0][0], "C1")
        self.assertIn("https://www.nasa.gov/scene-1", repairs[0][1])

    def test_repair_that_breaks_the_word_target_is_rejected(self):
        claim = {
            "id": "C2",
            "scene": 1,
            "sentence": "Forest moisture also returns to the air through leaves.",
            "source_urls": ["https://www.nasa.gov/scene-1"],
        }
        plan = {
            "script": "Short narration only.",
            "scenes": [
                {
                    "narration": (
                        "The Amazon basin contains extensive tropical rainforest habitat. "
                        "Forest moisture also returns to the air through leaves."
                    )
                }
            ],
            "fact_claims": [
                {
                    "scene": 1,
                    "sentence": "Forest moisture also returns to the air through leaves.",
                    "source_urls": ["https://www.nasa.gov/scene-1"],
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "production target"):
            fact_check._apply_claim_repair(
                plan, claim, {"sentence": "", "source_urls": []}, set()
            )

    def test_removal_that_would_empty_a_scene_is_rejected(self):
        plan = build_plan()
        pages = pages_for_plan(
            plan,
            {
                "https://www.nasa.gov/scene-1": page(
                    "https://www.nasa.gov/scene-1",
                    "A page about spacecraft engineering and orbital observations. " * 8,
                    "Unrelated NASA page",
                )
            },
        )

        def repair(_claim, _context, _plan):
            return {"sentence": "", "source_urls": []}

        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: pages[url]):
            with patch.object(fact_check, "discover_trusted_source_urls", return_value=[]):
                with patch.object(fact_check, "discover_scholarly_source_urls", return_value=[]):
                    with self.assertRaisesRegex(FactCheckError, "without narration"):
                        verify_documentary_facts(plan, passing_checker(), repair_claim=repair)

    def test_quantitative_c2_claim_is_repaired_instead_of_blocking_the_run(self):
        # Regression for the C2 failure: a numeric claim whose only relevant
        # passage came from one organisation was blocked before the bounded
        # repair mechanism could rewrite it, so no voice-over or video ran.
        quantitative = "The basin lost 17 percent of its forest cover in the study period."
        plan = build_plan({2: quantitative})
        pages = pages_for_plan(plan)
        replacement = (
            "The Amazon basin lost extensive forest cover across the study region "
            "during the measurement period."
        )
        repairs = []

        def repair(claim, context, _plan):
            repairs.append((claim["id"], dict(context["report"])))
            return {
                "sentence": replacement,
                "source_urls": ["https://www.nasa.gov/scene-2"],
            }

        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: pages[url]):
            with patch.object(
                fact_check,
                "_recover_independent_source_urls",
                return_value=([], "", ""),
            ):
                with patch.object(fact_check, "discover_trusted_source_urls", return_value=[]):
                    with patch.object(fact_check, "discover_scholarly_source_urls", return_value=[]):
                        report = verify_documentary_facts(
                            plan, passing_checker(), repair_claim=repair
                        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(repairs[0][0], "C2")
        self.assertEqual(
            repairs[0][1]["failure_reason"],
            "quantitative claim lacked a second independent organisation",
        )
        self.assertEqual(len(report["claim_repairs"]), 1)
        self.assertEqual(report["claim_repairs"][0]["claim_id"], "C2")
        self.assertEqual(report["claim_repairs"][0]["status"], "rewritten")
        self.assertEqual(plan["scenes"][1]["narration"], replacement)
        self.assertEqual(plan["fact_claims"][1]["sentence"], replacement)
        self.assertNotIn("17 percent", plan["script"])

    def test_repair_never_accepts_an_invented_source(self):
        plan = build_plan()
        pages = pages_for_plan(
            plan,
            {
                "https://www.nasa.gov/scene-1": page(
                    "https://www.nasa.gov/scene-1",
                    "A page about spacecraft engineering and orbital observations. " * 8,
                    "Unrelated NASA page",
                )
            },
        )

        def repair(_claim, _context, _plan):
            return {"sentence": "A new sentence.", "source_urls": ["http://untrusted.example/fact"]}

        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: pages[url]):
            with patch.object(fact_check, "discover_trusted_source_urls", return_value=[]):
                with patch.object(fact_check, "discover_scholarly_source_urls", return_value=[]):
                    with self.assertRaises(FactCheckError) as context:
                        verify_documentary_facts(plan, passing_checker(), repair_claim=repair)
        self.assertIn("trusted HTTPS allowlist", str(context.exception))

    def test_fact_audit_uses_a_stage_label_when_the_route_supports_one(self):
        plan = build_plan()
        pages = pages_for_plan(plan)
        stages = []
        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: pages[url]):
            report = verify_documentary_facts(plan, passing_checker(stages))
        self.assertEqual(report["status"], "passed")
        self.assertIn("fact_check_audit", stages)


class FreeProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        ai_router._DISABLED_PROVIDERS.clear()
        ai_router._DISABLED_ROUTES.clear()
        ai_router._COOLDOWN_UNTIL.clear()
        ai_router._PROVIDER_COOLDOWN_UNTIL.clear()
        ai_router._GEMINI_WORKING_MODEL.clear()
        ai_router._FREE_ONLY_WARNED.clear()

    def test_only_free_routes_survive_without_paid_opt_in(self):
        with patch.dict(
            os.environ,
            {
                "AI_FACT_PROVIDER_ORDER": "deepseek,openrouter,gemini,groq",
                "AI_ALLOW_PAID_PROVIDERS": "false",
                "DEEPSEEK_API_KEY": "test-deepseek",
                "OPENROUTER_API_KEY": "test-openrouter",
                "OPENROUTER_MODEL": "openai/gpt-4o",
                "OPENROUTER_TEXT_FALLBACK_MODEL": "baidu/ernie-5.5-flash-preview:free",
                "GEMINI_API_KEY": "test-gemini",
                "GROQ_API_KEY": "test-groq",
            },
            clear=True,
        ):
            routes = ai_router._routes("fact")
        providers = [route.provider for route in routes]
        self.assertNotIn("deepseek", providers)
        self.assertEqual(providers.count("gemini"), 1)
        self.assertEqual(providers.count("groq"), 1)
        openrouter_models = [r.model for r in routes if r.provider == "openrouter"]
        self.assertTrue(all(model.endswith(":free") for model in openrouter_models))
        self.assertNotIn("openai/gpt-4o", openrouter_models)

    def test_stage_isolated_gemini_rate_limit_falls_back_to_groq_for_fact(self):
        response = ai_router.requests.Response()
        response.status_code = 200
        response._content = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}
        ).encode()
        limited = ai_router.requests.Response()
        limited.status_code = 429
        limited._content = json.dumps({"error": {"message": "rate limited"}}).encode()
        with patch.dict(
            os.environ,
            {
                "AI_FACT_PROVIDER_ORDER": "gemini,groq",
                "AI_ALLOW_PAID_PROVIDERS": "false",
                "GEMINI_API_KEY": "test-gemini",
                "GROQ_API_KEY": "test-groq",
                "AI_RETRY_BASE_SECONDS": "0",
                "AI_ROUTER_MAX_PASSES": "1",
                "GEMINI_MODEL_FALLBACKS": "",
            },
            clear=True,
        ):
            with patch("ai_router.requests.post", side_effect=[limited, response]):
                value, provider, _model = ai_router.generate_fact_json(
                    "Return JSON", stage="fact_check_audit"
                )
        self.assertEqual(value, {"ok": True})
        self.assertEqual(provider, "groq")


class WorkflowReadinessTests(unittest.TestCase):
    def setUp(self):
        self.workflow = Path(__file__).resolve().parents[1] / ".github/workflows/render.yml"
        self.text = self.workflow.read_text(encoding="utf-8")

    def test_manual_dispatch_is_available(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("topic:", self.text)

    def test_free_provider_policy_is_pinned_in_the_workflow(self):
        self.assertIn('AI_ALLOW_PAID_PROVIDERS: "false"', self.text)
        self.assertIn("AI_FACT_PROVIDER_ORDER: gemini,groq,openrouter", self.text)
        self.assertIn('AI_ROUTER_MAX_PASSES: "4"', self.text)

    def test_tests_and_artifact_uploads_are_part_of_the_job(self):
        self.assertIn("python -m unittest discover -s tests -v", self.text)
        self.assertIn("documentary.mp4", self.text)
        self.assertIn("if: success()", self.text)
        self.assertIn("if: failure() || cancelled()", self.text)
        self.assertIn("failure_report.json", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
