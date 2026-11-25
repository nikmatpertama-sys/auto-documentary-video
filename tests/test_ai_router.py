
import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import ai_router


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = dict(headers or {})

    def json(self):
        return self._payload


class AIRouterTests(unittest.TestCase):
    def setUp(self):
        ai_router._DISABLED_PROVIDERS.clear()
        ai_router._DISABLED_ROUTES.clear()
        ai_router._COOLDOWN_UNTIL.clear()
        ai_router._PROVIDER_COOLDOWN_UNTIL.clear()
        ai_router._GEMINI_WORKING_MODEL.clear()
        ai_router._FREE_ONLY_WARNED.clear()

    def test_text_429_fails_over_to_deepseek(self):
        raw_plan = {"ok": True, "scenes": []}
        deepseek_response = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps(raw_plan)}}]
        })
        rate_limited = FakeResponse(429, {"error": {"message": "quota exceeded"}})

        logs = StringIO()
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq,deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True), patch("ai_router.requests.post", side_effect=[rate_limited, deepseek_response]) as post, redirect_stdout(logs):
            value, provider, model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, raw_plan)
        self.assertEqual(provider, "deepseek")
        self.assertEqual(model, "deepseek-flash")
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("test-groq", logs.getvalue())
        self.assertNotIn("test-deepseek", logs.getvalue())


    def test_fact_check_uses_independent_provider_order(self):
        response = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"checked": True})}}]
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "AI_FACT_PROVIDER_ORDER": "deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True), patch("ai_router.requests.post", return_value=response) as post:
            value, provider, model = ai_router.generate_fact_json("Verify claims")

        self.assertEqual(value, {"checked": True})
        self.assertEqual(provider, "deepseek")
        self.assertEqual(model, "deepseek-flash")
        self.assertIn("api.deepseek.com", post.call_args.args[0])

    def test_free_tier_mode_skips_the_prepaid_provider(self):
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "deepseek,groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "false",
            "GROQ_API_KEY": "test-groq",
        }, clear=True):
            providers = [route.provider for route in ai_router._routes("text")]

        self.assertEqual(providers, ["groq"])

    def test_prepaid_provider_is_registered_only_after_opt_in(self):
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "deepseek,groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
            "GROQ_API_KEY": "test-groq",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True):
            providers = [route.provider for route in ai_router._routes("text")]

        self.assertEqual(providers, ["deepseek", "groq"])

    def test_paid_openrouter_slug_is_skipped_in_free_tier_mode(self):
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "openrouter",
            "OPENROUTER_API_KEY": "test-openrouter",
            "OPENROUTER_MODEL": "openai/gpt-oss-120b",
            "OPENROUTER_TEXT_FALLBACK_MODEL": "baidu/ernie-5.5-flash-preview:free",
        }, clear=True):
            models = [route.model for route in ai_router._routes("text")]

        self.assertNotIn("openai/gpt-oss-120b", models)
        self.assertIn("baidu/ernie-5.5-flash-preview:free", models)

    def test_gemini_overloaded_model_falls_back_to_the_next_free_model(self):
        overloaded = FakeResponse(503, {"error": {"message": "high demand"}})
        valid = FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json.dumps({"ready": True})}]}}]
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "gemini",
            "GEMINI_API_KEY": "test-gemini-key",
            "GEMINI_MODEL": "gemini-3.5-flash-lite",
            "GEMINI_MODEL_FALLBACKS": "gemini-3.8-flash,gemini-2.5-flash",
        }, clear=True), patch(
            "ai_router.requests.post", side_effect=[overloaded, valid]
        ) as post:
            value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "gemini")
        self.assertIn("gemini-3.5-flash-lite", post.call_args_list[0].args[0])
        self.assertIn("gemini-3.8-flash", post.call_args_list[1].args[0])

    def test_gemini_retired_model_falls_back_to_the_next_free_model(self):
        missing = FakeResponse(404, {"error": {"message": "models/gemini-3.8-flash is not found"}})
        valid = FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json.dumps({"ready": True})}]}}]
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "gemini",
            "GEMINI_API_KEY": "test-gemini-key",
            "GEMINI_MODEL": "gemini-3.8-flash",
            "GEMINI_MODEL_FALLBACKS": "gemini-3.5-flash-lite,gemini-2.5-flash",
        }, clear=True), patch(
            "ai_router.requests.post", side_effect=[missing, valid]
        ) as post:
            value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "gemini")
        self.assertIn("gemini-3.8-flash", post.call_args_list[0].args[0])
        self.assertIn("gemini-3.5-flash-lite", post.call_args_list[1].args[0])

    def test_default_fact_order_never_includes_the_prepaid_provider(self):
        with patch.dict(os.environ, {
            "GROQ_API_KEY": "test-groq",
            "GEMINI_API_KEY": "test-gemini",
            "OPENROUTER_API_KEY": "test-openrouter",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True):
            providers = [route.provider for route in ai_router._routes("fact")]

        # A zero-cost setup must never reach the prepaid balance route by
        # default, not even when paid usage has been opted into.
        self.assertNotIn("deepseek", providers)
        self.assertEqual(providers[0], "gemini")

    def test_insufficient_balance_is_reported_as_billing_not_outage(self):
        empty_balance = FakeResponse(402, {
            "error": {"message": "Insufficient Balance"}
        })
        valid = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": True})}}]
        })
        logs = StringIO()
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "deepseek,groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
            "GROQ_API_KEY": "test-groq",
        }, clear=True), patch(
            "ai_router.requests.post", side_effect=[empty_balance, valid]
        ), redirect_stdout(logs):
            value, provider, _model = ai_router.generate_json("Return one JSON object")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "groq")
        self.assertIn("insufficient balance/payment", logs.getvalue())
        self.assertNotIn("temporarily unavailable", logs.getvalue())

    def test_combined_provider_failure_pattern_recovers_at_openrouter(self):
        # Mirrors the failed Actions run: Groq returns a schema-invalid plan,
        # DeepSeek has no balance, Gemini is temporarily unavailable, and the
        # first OpenRouter response is malformed. The bounded repair calls
        # should let OpenRouter recover without weakening plan validation.
        groq_invalid_first = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": False})}}]
        })
        groq_invalid_retry = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": False})}}]
        })
        deepseek_no_balance = FakeResponse(402, {
            "error": {"message": "Insufficient Balance"}
        })
        gemini_unavailable = FakeResponse(503, {
            "error": {"message": "service is experiencing high demand"}
        })
        openrouter_bad_json = FakeResponse(200, {
            "choices": [{"message": {"content": "not a JSON object"}}]
        })
        openrouter_repaired = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": True})}}]
        })

        def validator(value):
            if value.get("ready") is not True:
                raise ValueError(
                    "Scene 3 visual_focus is not represented by its primary visual_query."
                )
            return value

        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq,deepseek,gemini,openrouter",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
            "GEMINI_API_KEY": "test-gemini",
            # One Gemini model per route keeps this case about provider
            # failover; model-level failover has its own test.
            "GEMINI_MODEL_FALLBACKS": "",
            "OPENROUTER_API_KEY": "test-openrouter",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[
                groq_invalid_first,
                groq_invalid_retry,
                deepseek_no_balance,
                gemini_unavailable,
                openrouter_bad_json,
                openrouter_repaired,
            ],
        ) as post:
            value, provider, _model = ai_router.generate_json(
                "Return the source-backed plan", validator=validator
            )

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "openrouter")
        self.assertEqual(post.call_count, 6)
        self.assertEqual(
            post.call_args_list[-1].kwargs["json"]["response_format"],
            {"type": "json_object"},
        )

    def test_valid_visual_reject_does_not_trigger_provider_fallback(self):
        reject = {"relevant": False, "confidence": 0.94, "reason": "Requested animal not visible"}
        response = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps(reject)}}]
        })
        with patch.dict(os.environ, {
            "AI_VISION_PROVIDER_ORDER": "groq,openrouter",
            "GROQ_API_KEY": "test-groq",
            "OPENROUTER_API_KEY": "test-openrouter",
        }, clear=True), patch("ai_router.requests.post", return_value=response) as post:
            value = ai_router.verify_images([b"jpeg-frame-a", b"jpeg-frame-b"], "Check the animal")

        self.assertFalse(value["accepted"])
        self.assertEqual(value["provider"], "groq")
        self.assertEqual(post.call_count, 1)

    def test_all_vision_routes_rate_limited_is_distinct_error(self):
        rate_limited = FakeResponse(429, {"error": {"message": "daily quota exhausted"}})
        with patch.dict(os.environ, {
            "AI_VISION_PROVIDER_ORDER": "groq,deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True), patch("ai_router.requests.post", return_value=rate_limited):
            with self.assertRaises(ai_router.VisionServiceUnavailable):
                ai_router.verify_images([b"frame"], "Check footage")


    def test_transient_server_error_is_retried_once(self):
        raw_plan = {"ready": True}
        temporary = FakeResponse(503, {"error": {"message": "service unavailable"}})
        healthy = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps(raw_plan)}}]
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True), patch("ai_router.requests.post", side_effect=[temporary, healthy]) as post, patch("ai_router.time.sleep") as sleep:
            value, provider, model = ai_router.generate_json("Return JSON")
        self.assertEqual(value, raw_plan)
        self.assertEqual(provider, "deepseek")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5)


    def test_timeout_fails_over_to_next_provider(self):
        healthy = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": True})}}]
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq,deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[ai_router.requests.exceptions.Timeout("read timeout"), healthy],
        ) as post:
            value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "deepseek")
        self.assertEqual(post.call_count, 2)

    def test_gemini_uses_generate_content_and_header_key(self):
        valid = {"relevant": True, "confidence": 0.93, "reason": "Requested subject visible"}
        response = FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json.dumps(valid)}]}}]
        })
        with patch.dict(os.environ, {
            "AI_VISION_PROVIDER_ORDER": "gemini",
            "GEMINI_API_KEY": "test-gemini-key",
        }, clear=True), patch("ai_router.requests.post", return_value=response) as post:
            result = ai_router.verify_images([b"frame-a", b"frame-b"], "Verify this scene")

        args, kwargs = post.call_args
        self.assertIn(":generateContent", args[0])
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-gemini-key")
        self.assertNotIn("test-gemini-key", args[0])
        parts = kwargs["json"]["contents"][0]["parts"]
        self.assertTrue(any("inline_data" in part for part in parts))
        self.assertTrue(result["accepted"])

    def test_validator_failure_retries_same_provider_once_with_targeted_repair(self):
        invalid = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": False})}}]
        })
        valid = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": True})}}]
        })

        def validator(value):
            if value.get("ready") is not True:
                raise ValueError("Scene 3 visual_focus is not represented by its primary visual_query.")
            return value

        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "GROQ_API_KEY": "test-groq",
            "AI_GROQ_REPAIR_WAIT_SECONDS": "0",
        }, clear=True), patch(
            "ai_router.requests.post", side_effect=[invalid, valid]
        ) as post:
            value, provider, _model = ai_router.generate_json(
                "Return a documentary plan", validator=validator
            )

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "groq")
        self.assertEqual(post.call_count, 2)
        first_payload = post.call_args_list[0].kwargs["json"]
        retry_payload = post.call_args_list[1].kwargs["json"]
        # Groq rejects truncated json_object output server-side with HTTP 400,
        # so the router validates and repairs the JSON itself and asks for low
        # reasoning effort instead.
        self.assertNotIn("response_format", first_payload)
        self.assertEqual(first_payload["reasoning_effort"], "low")
        retry_prompt = retry_payload["messages"][1]["content"]
        self.assertIn("STRICT OUTPUT REPAIR REQUIRED", retry_prompt)
        self.assertIn("visual_focus", retry_prompt)

    def test_unrepaired_groq_schema_falls_through_to_deepseek(self):
        invalid_one = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": False})}}]
        })
        invalid_two = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": False})}}]
        })
        valid_deepseek = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": True})}}]
        })

        def validator(value):
            if value.get("ready") is not True:
                raise ValueError("plan schema rejected")
            return value

        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq,deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
            "AI_GROQ_REPAIR_WAIT_SECONDS": "0",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[invalid_one, invalid_two, valid_deepseek],
        ) as post:
            value, provider, _model = ai_router.generate_json(
                "Return a documentary plan", validator=validator
            )

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "deepseek")
        self.assertEqual(post.call_count, 3)
        self.assertIn("api.groq.com", post.call_args_list[0].args[0])
        self.assertIn("api.groq.com", post.call_args_list[1].args[0])
        self.assertIn("api.deepseek.com", post.call_args_list[2].args[0])

    def test_openrouter_text_route_requests_json_mode(self):
        valid = {"title": "Test", "scenes": []}
        response = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps(valid)}}]
        })
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "openrouter",
            "OPENROUTER_API_KEY": "test-openrouter",
        }, clear=True), patch("ai_router.requests.post", return_value=response) as post:
            value, provider, _model = ai_router.generate_json("Return one JSON object")

        self.assertEqual(value, valid)
        self.assertEqual(provider, "openrouter")
        self.assertEqual(
            post.call_args.kwargs["json"]["response_format"],
            {"type": "json_object"},
        )

    def test_openrouter_dynamic_empty_output_falls_back_to_ernie(self):
        # Mirrors the live failure: a dynamic free route can spend its entire
        # output budget on reasoning and return empty assistant content.
        empty_reasoning_only = FakeResponse(200, {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "internal reasoning only",
                    "reasoning_details": [],
                },
                "finish_reason": "length",
            }]
        })
        valid = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps({"ready": True})}}]
        })

        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "openrouter",
            "OPENROUTER_API_KEY": "test-openrouter",
            "OPENROUTER_MODEL": "openrouter/free",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[
                empty_reasoning_only,
                empty_reasoning_only,
                empty_reasoning_only,
                valid,
            ],
        ) as post:
            value, provider, model = ai_router.generate_json("Return one complete JSON object")

        self.assertEqual(value, {"ready": True})
        self.assertEqual(provider, "openrouter")
        self.assertEqual(model, "baidu/ernie-5.5-flash-preview:free")
        self.assertEqual(post.call_count, 4)
        # An empty answer is a budget that was too small, not too large: the
        # same route is retried once with more room before the fallback runs.
        self.assertGreater(
            post.call_args_list[1].kwargs["json"]["max_tokens"],
            post.call_args_list[0].kwargs["json"]["max_tokens"],
        )
        fallback_payload = post.call_args_list[-1].kwargs["json"]
        self.assertEqual(fallback_payload["model"], "baidu/ernie-5.5-flash-preview:free")
        self.assertEqual(fallback_payload["reasoning"], {"enabled": False})
        self.assertEqual(fallback_payload["response_format"], {"type": "json_object"})

    def test_transient_retry_preserves_first_failure_diagnostic(self):
        first = FakeResponse(503, {"error": {"message": "service unavailable"}})
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "gemini",
            "GEMINI_API_KEY": "test-gemini",
            "GEMINI_MODEL_FALLBACKS": "",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[first, ai_router.requests.exceptions.Timeout("read timeout")],
        ), patch("ai_router.time.sleep"):
            with self.assertRaises(ai_router.ProviderRoutingError) as caught:
                ai_router.generate_json("Return JSON")

        message = str(caught.exception)
        self.assertIn("HTTP 503", message)
        self.assertIn("Timeout", message)

    def test_text_route_cooldown_does_not_block_same_model_vision(self):
        temporary = FakeResponse(503, {"error": {"message": "text service unavailable"}})
        valid_vision = {"relevant": True, "confidence": 0.93, "reason": "Requested subject is visible"}
        vision_response = FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json.dumps(valid_vision)}]}}]
        })

        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "gemini",
            "AI_VISION_PROVIDER_ORDER": "gemini",
            "GEMINI_API_KEY": "test-gemini-key",
            "GEMINI_MODEL": "gemini-3.8-flash",
            "GEMINI_VISION_MODEL": "gemini-3.8-flash",
            "GEMINI_MODEL_FALLBACKS": "",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[temporary, temporary, vision_response],
        ) as post, patch("ai_router.time.sleep"):
            with self.assertRaises(ai_router.ProviderRoutingError):
                ai_router.generate_json("Return a JSON plan")

            # The text call has cooled down the same model name. Its Vision route
            # must remain usable because the two tasks have independent health state.
            result = ai_router.verify_images([b"frame-a", b"frame-b"], "Verify this frame")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual(post.call_count, 3)

    def test_echoed_api_key_is_redacted_from_error_logs(self):
        from contextlib import redirect_stdout
        from io import StringIO
        bad_request = FakeResponse(400, {"error": {"message": "invalid key test-deepseek"}})
        logs = StringIO()
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True), patch("ai_router.requests.post", return_value=bad_request), redirect_stdout(logs):
            with self.assertRaises(ai_router.ProviderRoutingError):
                ai_router.generate_json("Return JSON")
        self.assertNotIn("test-deepseek", logs.getvalue())

    def test_missing_keys_is_clear_provider_error(self):
        with patch.dict(os.environ, {"AI_TEXT_PROVIDER_ORDER": "groq"}, clear=True):
            with self.assertRaises(ai_router.ProviderRoutingError):
                ai_router.generate_json("Return JSON")


    def test_vision_confidence_floor_cannot_be_lowered_by_environment(self):
        from unittest.mock import patch
        from ai_router import _vision_min_confidence

        with patch.dict("os.environ", {"FOOTAGE_VISION_MIN_CONFIDENCE": "0.10"}, clear=False):
            self.assertEqual(_vision_min_confidence(), 0.72)
        with patch.dict("os.environ", {"FOOTAGE_VISION_MIN_CONFIDENCE": "0.90"}, clear=False):
            self.assertEqual(_vision_min_confidence(), 0.90)


class AIRouterFailoverTests(unittest.TestCase):
    """Rate limits must pause a provider, not delete it from the rest of the run."""

    def setUp(self):
        ai_router._DISABLED_PROVIDERS.clear()
        ai_router._DISABLED_ROUTES.clear()
        ai_router._COOLDOWN_UNTIL.clear()
        ai_router._PROVIDER_COOLDOWN_UNTIL.clear()
        ai_router._GEMINI_WORKING_MODEL.clear()
        ai_router._FREE_ONLY_WARNED.clear()

    def _ok(self, payload):
        return FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps(payload)}}]
        })

    def test_deepseek_is_not_used_for_vision_without_explicit_model(self):
        with patch.dict(os.environ, {
            "AI_VISION_PROVIDER_ORDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True):
            self.assertEqual(ai_router._routes("vision"), [])
            self.assertEqual(ai_router.configured_vision_provider_names(), [])
            with self.assertRaises(ai_router.VisionServiceUnavailable):
                ai_router.verify_images([b"frame"], "Check footage")

    def test_deepseek_vision_route_requires_explicit_model(self):
        with patch.dict(os.environ, {
            "AI_VISION_PROVIDER_ORDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
            "DEEPSEEK_VISION_MODEL": "deepseek-vl",
        }, clear=True):
            routes = ai_router._routes("vision")
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].model, "deepseek-vl")

    def test_rate_limit_429_pauses_provider_then_reuses_it(self):
        rate_limited = FakeResponse(429, {"error": {"message": "too many requests"}})
        env = {
            "AI_TEXT_PROVIDER_ORDER": "groq,deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[rate_limited, self._ok({"first": True}), self._ok({"second": True})],
        ) as post:
            value, provider, _model = ai_router.generate_json("Return JSON")
            self.assertEqual(provider, "deepseek")
            self.assertEqual(value, {"first": True})
            # The provider is paused, not permanently disabled.
            self.assertNotIn(("text", "groq"), ai_router._DISABLED_PROVIDERS)
            self.assertIn(("text", "groq"), ai_router._PROVIDER_COOLDOWN_UNTIL)

            # Once the pause expires the same provider must be usable again.
            ai_router._PROVIDER_COOLDOWN_UNTIL.clear()
            ai_router._COOLDOWN_UNTIL.clear()
            value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(provider, "groq")
        self.assertEqual(value, {"second": True})
        self.assertEqual(post.call_count, 3)

    def test_exhausted_quota_429_disables_provider_for_the_run(self):
        exhausted = FakeResponse(429, {"error": {"message": "You exceeded your current quota"}})
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq,deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "AI_ALLOW_PAID_PROVIDERS": "true",
        }, clear=True), patch(
            "ai_router.requests.post",
            side_effect=[exhausted, self._ok({"ok": True})],
        ):
            _value, provider, _model = ai_router.generate_json("Return JSON")

        self.assertEqual(provider, "deepseek")
        self.assertIn(("text", "groq"), ai_router._DISABLED_PROVIDERS)

    def test_retry_after_header_bounds_the_pause(self):
        rate_limited = FakeResponse(
            429,
            {"error": {"message": "slow down"}},
            headers={"Retry-After": "42"},
        )
        with patch.dict(os.environ, {
            "AI_TEXT_PROVIDER_ORDER": "groq",
            "GROQ_API_KEY": "test-groq",
        }, clear=True), patch("ai_router.requests.post", return_value=rate_limited):
            with self.assertRaises(ai_router.ProviderRoutingError):
                ai_router.generate_json("Return JSON")

        remaining = (
            ai_router._PROVIDER_COOLDOWN_UNTIL[("text", "groq")]
            - __import__("time").monotonic()
        )
        self.assertLessEqual(remaining, 42.0)
        self.assertGreater(remaining, 30.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
