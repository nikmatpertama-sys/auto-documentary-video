"""Phase 1 provider-stability regression tests.

Each case mirrors a real failed Actions run: an oversized token budget rejected
with HTTP 413, a reasoning model that returns no content with
``finish_reason=length``, a repeated 503, and a rate-limited provider whose
cooldown used to delete it for the rest of the run.
"""

import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import ai_router
import main


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = dict(headers or {})

    def json(self):
        return self._payload


def _ok(payload):
    return FakeResponse(200, {
        "choices": [{"message": {"content": json.dumps(payload)}}]
    })


def _empty_length():
    return FakeResponse(200, {
        "choices": [{
            "message": {"role": "assistant", "content": "", "reasoning": "thinking"},
            "finish_reason": "length",
        }]
    })


class ProviderStabilityTests(unittest.TestCase):
    def setUp(self):
        ai_router._DISABLED_PROVIDERS.clear()
        ai_router._DISABLED_ROUTES.clear()
        ai_router._COOLDOWN_UNTIL.clear()
        ai_router._PROVIDER_COOLDOWN_UNTIL.clear()
        ai_router._COMPLETION_SCALE.clear()
        ai_router._GEMINI_WORKING_MODEL.clear()
        ai_router._FREE_ONLY_WARNED.clear()

    def test_413_shrinks_the_budget_and_recovers_on_the_same_route(self):
        too_large = FakeResponse(413, {
            "error": {"message": "Request too large: 8640 tokens exceeds 8000 TPM"}
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "GROQ_API_KEY": "test-groq",
            "AI_TEXT_TOKEN_CEILING": "8000",
        }, clear=True), patch(
            "ai_router.requests.post", side_effect=[too_large, _ok({"ready": True})]
        ) as post:
            value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "groq")
        self.assertEqual(post.call_count, 2)
        first_budget = post.call_args_list[0].kwargs["json"]["max_completion_tokens"]
        retry_budget = post.call_args_list[1].kwargs["json"]["max_completion_tokens"]
        self.assertLess(retry_budget, first_budget)
        # A token-ceiling rejection is not a dead provider.
        self.assertNotIn(("text", "groq:openai/gpt-oss-120b"), ai_router._DISABLED_ROUTES)
        self.assertNotIn(("text", "groq"), ai_router._DISABLED_PROVIDERS)

    def test_groq_completion_budget_fits_the_token_ceiling(self):
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "GROQ_API_KEY": "test-groq",
            "AI_TEXT_TOKEN_CEILING": "8000",
        }, clear=True), patch(
            "ai_router.requests.post", return_value=_ok({"ready": True})
        ) as post:
            ai_router.generate_json("x" * 4000)

        payload = post.call_args.kwargs["json"]
        prompt_tokens = max(1, len("x" * 4000) // 4)
        self.assertLessEqual(
            prompt_tokens + payload["max_completion_tokens"], 8000
        )

    def test_empty_output_retries_the_same_route_with_a_larger_budget(self):
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "openrouter",
            "OPENROUTER_API_KEY": "test-openrouter",
            "OPENROUTER_MODEL": "openrouter/free",
            "AI_GROQ_REPAIR_WAIT_SECONDS": "0",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[_empty_length(), _ok({"ready": True})],
        ) as post:
            value, provider, model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual((provider, model), ("openrouter", "openrouter/free"))
        self.assertEqual(post.call_count, 2)
        # The whole answer was consumed by hidden reasoning, so a smaller budget
        # could never contain the JSON: the retry must reserve more room.
        self.assertGreater(
            post.call_args_list[1].kwargs["json"]["max_tokens"],
            post.call_args_list[0].kwargs["json"]["max_tokens"],
        )
        self.assertEqual(
            post.call_args_list[0].kwargs["json"]["reasoning"], {"enabled": False}
        )
        self.assertNotIn(
            ("text", "openrouter:openrouter/free"), ai_router._DISABLED_ROUTES
        )

    def test_incomplete_json_retries_the_same_route_with_a_smaller_budget(self):
        def _partial_json():
            return FakeResponse(200, {
                "choices": [{"message": {"content": '{"ready": tru'}}]
            })

        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "openrouter",
            "OPENROUTER_API_KEY": "test-openrouter",
            "OPENROUTER_MODEL": "openrouter/free",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[_partial_json(), _ok({"ready": True})],
        ) as post:
            value, provider, model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual((provider, model), ("openrouter", "openrouter/free"))
        self.assertEqual(post.call_count, 2)
        self.assertLess(
            post.call_args_list[1].kwargs["json"]["max_tokens"],
            post.call_args_list[0].kwargs["json"]["max_tokens"],
        )

    def test_repeated_503_gets_exponential_backoff_between_passes(self):
        unavailable = FakeResponse(503, {"error": {"message": "high demand"}})
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
            "AI_ROUTER_MAX_PASSES": "4",
            "AI_RETRY_BASE_SECONDS": "5",
            "AI_RETRY_MAX_SECONDS": "60",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[unavailable, unavailable, unavailable, _ok({"ready": True})],
        ) as post, patch("ai_router.time.sleep") as sleep:
            value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "deepseek")
        self.assertEqual(post.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5.0, 10.0, 20.0])

    def test_declared_rate_limit_wait_is_honoured_within_the_wait_budget(self):
        rate_limited = FakeResponse(
            429,
            {"error": {"message": "slow down"}},
            headers={"Retry-After": "30"},
        )
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "GROQ_API_KEY": "test-groq",
            "AI_ROUTER_WAIT_BUDGET_SECONDS": "60",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[rate_limited, _ok({"ready": True})],
        ) as post, patch("ai_router.time.sleep") as sleep:
            value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "groq")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 30.0, places=1)

    def test_wait_beyond_the_budget_fails_over_without_blocking(self):
        rate_limited = FakeResponse(
            429,
            {"error": {"message": "slow down"}},
            headers={"Retry-After": "600"},
        )
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "GROQ_API_KEY": "test-groq",
            "AI_ROUTER_WAIT_BUDGET_SECONDS": "60",
        }, clear=True), patch(
            "ai_router.requests.post", return_value=rate_limited
        ) as post, patch("ai_router.time.sleep") as sleep:
            with self.assertRaises(ai_router.ProviderRoutingError):
                ai_router.generate_json("Return JSON")

        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertIn(("text", "groq"), ai_router._PROVIDER_COOLDOWN_UNTIL)

    def test_400_structured_output_rejection_does_not_disable_the_route(self):
        invalid_json = FakeResponse(400, {
            "error": {"message": "Failed to validate JSON. Please adjust your prompt."}
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "GROQ_API_KEY": "test-groq",
        }, clear=True), patch(
            "ai_router.requests.post", return_value=invalid_json
        ):
            with self.assertRaises(ai_router.ProviderRoutingError):
                ai_router.generate_json("Return JSON")

        self.assertNotIn(
            ("text", "groq:openai/gpt-oss-120b"), ai_router._DISABLED_ROUTES
        )

    def test_one_failed_stage_does_not_disable_the_next_stage(self):
        unavailable = FakeResponse(503, {"error": {"message": "high demand"}})
        valid = FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json.dumps({"ready": True})}]}}]
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "gemini",
            "GEMINI_API_KEY": "test-gemini",
            "GEMINI_MODEL_FALLBACKS": "",
            "AI_ROUTER_MAX_PASSES": "2",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[unavailable, unavailable, valid],
        ) as post, patch("ai_router.time.sleep"):
            logs = StringIO()
            with redirect_stdout(logs):
                with self.assertRaises(ai_router.ProviderRoutingError):
                    ai_router.generate_json("Name article titles", stage="source_pool")

                value, provider, _model = ai_router.generate_json(
                    "Return the plan", stage="script_generation"
                )

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "gemini")
        self.assertEqual(post.call_count, 3)


class DocumentaryPromptBudgetTests(unittest.TestCase):
    POOL = [
        f"https://www.example{i}.gov/article-about-beavers-and-rivers"
        for i in range(12)
    ]

    def test_prompt_stays_within_the_planner_token_budget(self):
        prompt = main.documentary_prompt(
            "How beavers reshape entire river ecosystems", self.POOL
        )
        # The planner must fit in a free-tier request together with its
        # completion budget, so the prompt stays well under ~2,000 tokens.
        self.assertLess(len(prompt) // 4, 2000)
        self.assertLess(len(prompt), 8000)

    def test_prompt_keeps_every_enforced_requirement(self):
        prompt = main.documentary_prompt("Beavers", self.POOL)
        for required in (
            "Exactly 8 scenes",
            "160-185",
            "visual_focus",
            "at least half of them must appear",
            "TWO independent trusted hosts",
            "copied exactly from this list",
            "Never invent a source",
            "map, chart, graph",
            "curiosity hook of at most 12 words",
        ):
            self.assertIn(required, prompt)
        for url in self.POOL:
            self.assertIn(url, prompt)


class StagedPlanRecoveryTests(unittest.TestCase):
    """The two-request fallback that replaced a dead single-shot plan call."""

    POOL = ["https://www.noaa.gov/rain", "https://www.usgs.gov/water"]

    @staticmethod
    def _narration(index):
        return " ".join(f"scene{index}word{position}" for position in range(21))

    def _outline(self):
        scenes = []
        for index in range(1, 9):
            narration = self._narration(index)
            scenes.append({
                "narration": narration,
                "visual_focus": "rain forest canopy",
                "visual_query": "rain forest canopy",
                "visual_queries": ["rain on leaves", "forest mist"],
            })
        return {"title": "Rain", "hook": "A hook", "scenes": scenes}

    def _claims(self, start_index):
        return {"fact_claims": [
            {
                "scene": start_index + offset + 1,
                "sentence": self._narration(start_index + offset + 1),
                "source_urls": self.POOL,
            }
            for offset in range(4)
        ]}

    def test_staged_plan_assembles_the_outline_and_both_claim_halves(self):
        outline = self._outline()
        captured = {}

        def validate_plan(candidate):
            captured["candidate"] = candidate
            return candidate

        with patch.object(
            main,
            "generate_json",
            side_effect=[
                (outline, "gemini", "gemini-3.5-flash-lite"),
                (self._claims(0), "gemini", "gemini-3.5-flash-lite"),
                (self._claims(4), "gemini", "gemini-3.5-flash-lite"),
            ],
        ) as generate:
            plan, provider, model = main.staged_plan("Rain", self.POOL, validate_plan)

        self.assertEqual(generate.call_count, 3)
        self.assertEqual((provider, model), ("gemini", "gemini-3.5-flash-lite"))
        self.assertEqual(len(plan["scenes"]), 8)
        self.assertEqual(len(captured["candidate"]["fact_claims"]), 8)
        self.assertEqual(plan["title"], "Rain")
        # Narration and citation are separate requests so that neither has to
        # fit both a script and its sources into one completion budget.
        stages = [call.kwargs["stage"] for call in generate.call_args_list]
        self.assertEqual(stages, ["script_outline", "claim_sourcing", "claim_sourcing"])

    def test_visual_query_is_repaired_instead_of_rejecting_the_plan(self):
        repaired = main.align_visual_query_to_focus(
            "woodland downpour footage", "rain forest canopy"
        )

        self.assertEqual(repaired.split()[:3], ["rain", "forest", "canopy"])
        focus_words = set(main.normalize_words("rain forest canopy"))
        self.assertTrue(focus_words.issubset(set(main.normalize_words(repaired))))


class EvidenceBriefTests(unittest.TestCase):
    """The planner must read the source passages the audit will judge."""

    TOPIC = "How beaver dams create wetlands"

    def test_brief_keeps_topic_related_passages_and_drops_unusable_pages(self):
        pages = {
            "https://www.usgs.gov/beaver-dams": {
                "text": "Beaver dams create wetlands.",
                "title": "Beaver dams",
                "url": "https://www.usgs.gov/beaver-dams",
            },
            "https://www.noaa.gov/wetlands": {
                "text": "Wetlands store water.",
                "title": "Wetlands",
                "url": "https://www.noaa.gov/wetlands",
            },
            "https://www.nasa.gov/phylogeny": {
                "text": "Rodent phylogeny.",
                "title": "Phylogeny",
                "url": "https://www.nasa.gov/phylogeny",
            },
        }

        def fake_fetch(url):
            return pages[url]

        def fake_excerpt(page_text, claim, max_chars=420, required_numbers=()):
            if "Rodent" in page_text:
                raise main.FactCheckError("no lexical anchor for this claim")
            return page_text

        with patch.object(main, "fetch_source_page", side_effect=fake_fetch), patch.object(
            main, "select_relevant_excerpt", side_effect=fake_excerpt
        ):
            brief = main.build_evidence_brief(self.TOPIC, list(pages))

        urls = [item["url"] for item in brief]
        self.assertIn("https://www.usgs.gov/beaver-dams", urls)
        self.assertNotIn("https://www.nasa.gov/phylogeny", urls)
        self.assertEqual(brief[0]["url"], "https://www.usgs.gov/beaver-dams")

    def test_prompt_shows_the_fetched_passage_not_only_the_url(self):
        evidence = [{
            "url": "https://www.usgs.gov/beaver-dams",
            "title": "Beaver dams",
            "excerpt": "Dams slow water and create ponds.",
        }]
        prompt = main.documentary_prompt(self.TOPIC, [evidence[0]["url"]], evidence)

        self.assertIn("Dams slow water and create ponds.", prompt)
        self.assertIn("https://www.usgs.gov/beaver-dams", prompt)
        self.assertIn("FETCHED PASSAGE", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
