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

    def test_truncated_output_retries_the_same_route_with_a_smaller_budget(self):
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
        self.assertLess(
            post.call_args_list[1].kwargs["json"]["max_tokens"],
            post.call_args_list[0].kwargs["json"]["max_tokens"],
        )
        self.assertNotIn(
            ("text", "openrouter:openrouter/free"), ai_router._DISABLED_ROUTES
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
