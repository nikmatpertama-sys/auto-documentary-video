"""Tests for the capacity fixes: DeepSeek routing and repair length grace.

Two production runs aborted with "All configured AI providers failed" while a
DEEPSEEK_API_KEY secret sat unused, and a fact-check repair was thrown away
because the corrected script was shorter than the length gate. Both are pinned
here.
"""

import unittest
from unittest import mock

import main


def _plan(scenes=6):
    narration = (
        "Ants live in soil and build nests under stones where the queen lays "
        "her eggs and workers feed the brood every day."
    )
    scene_list = []
    for index in range(scenes):
        scene_list.append({
            "narration": narration,
            "visual_query": "ant colony close",
            "visual_queries": ["ant colony"],
            "source_label": "Ant",
            "source_url": "https://en.wikipedia.org/wiki/Ant",
        })
    script = " ".join(scene["narration"] for scene in scene_list)
    return {"title": "Ants", "hook": "What if a colony thinks?", "script": script, "scenes": scene_list}


class DeepSeekRoutingTests(unittest.TestCase):
    def test_deepseek_uses_its_own_endpoint_and_model(self):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            response = mock.Mock(status_code=200, text="")
            response.json.return_value = {
                "choices": [{"message": {"content": '{"ok": true}'}}],
            }
            return response

        with mock.patch.object(main, "post_with_transient_retry", side_effect=fake_post), \
                mock.patch.object(main, "_observe_provider_response"):
            result = main.call_openai_compatible("DeepSeek", "key", "deepseek-chat", "prompt")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["payload"]["model"], "deepseek-chat")
        self.assertEqual(captured["payload"]["max_tokens"], 6000)

    def test_deepseek_is_a_plan_route_when_configured(self):
        with mock.patch.object(main, "GROQ_KEYS", []), \
                mock.patch.object(main, "OPENROUTER_KEYS", []), \
                mock.patch.object(main, "GEMINI_KEYS", []), \
                mock.patch.object(main, "CLOUDFLARE_CREDENTIALS", []), \
                mock.patch.object(main, "DEEPSEEK_KEYS", ["deepseek-key"]):
            plan = _plan()
            with mock.patch.object(main, "call_openai_compatible", return_value=plan) as caller:
                result = main.gemini_plan("Ant colony life cycle", research_brief="Ants live in colonies.")
        self.assertEqual(result["title"], "Ants")
        self.assertEqual(caller.call_args[0][0], "DeepSeek")


class RepairLengthGraceTests(unittest.TestCase):
    def test_short_but_valid_plan_is_accepted_at_the_repair_floor(self):
        plan = _plan(scenes=4)
        self.assertLess(len(plan["script"].split()), 120)
        with self.assertRaises(RuntimeError):
            main.validate_plan_candidate(dict(plan))
        accepted = main.validate_plan_candidate(dict(plan), min_words=main.MIN_REPAIRED_WORDS)
        self.assertTrue(accepted["scenes"])

    def test_repair_floor_allows_dropping_unsupported_sentences(self):
        self.assertLess(main.MIN_REPAIRED_WORDS, 120)


if __name__ == "__main__":
    unittest.main()
