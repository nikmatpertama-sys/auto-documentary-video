
import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import ai_router


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class AIRouterTests(unittest.TestCase):
    def setUp(self):
        ai_router._DISABLED_PROVIDERS.clear()
        ai_router._DISABLED_ROUTES.clear()
        ai_router._COOLDOWN_UNTIL.clear()

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
        }, clear=True), patch("ai_router.requests.post", return_value=response) as post:
            value, provider, model = ai_router.generate_fact_json("Verify claims")

        self.assertEqual(value, {"checked": True})
        self.assertEqual(provider, "deepseek")
        self.assertEqual(model, "deepseek-flash")
        self.assertIn("api.deepseek.com", post.call_args.args[0])

    def test_valid_visual_reject_does_not_trigger_provider_fallback(self):
        reject = {"relevant": False, "confidence": 0.94, "reason": "Requested animal not visible"}
        response = FakeResponse(200, {
            "choices": [{"message": {"content": json.dumps(reject)}}]
        })
        with patch.dict(os.environ, {
            "AI_VISION_PROVIDER_ORDER": "deepseek,gemini",
            "DEEPSEEK_API_KEY": "test-deepseek",
            "GEMINI_API_KEY": "test-gemini",
        }, clear=True), patch("ai_router.requests.post", return_value=response) as post:
            value = ai_router.verify_images([b"jpeg-frame-a", b"jpeg-frame-b"], "Check the animal")

        self.assertFalse(value["accepted"])
        self.assertEqual(value["provider"], "deepseek")
        self.assertEqual(post.call_count, 1)

    def test_all_vision_routes_rate_limited_is_distinct_error(self):
        rate_limited = FakeResponse(429, {"error": {"message": "daily quota exhausted"}})
        with patch.dict(os.environ, {
            "AI_VISION_PROVIDER_ORDER": "groq,deepseek",
            "GROQ_API_KEY": "test-groq",
            "DEEPSEEK_API_KEY": "test-deepseek",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
