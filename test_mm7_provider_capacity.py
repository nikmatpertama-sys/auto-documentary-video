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


class PlanLengthGraceTests(unittest.TestCase):
    def test_short_draft_is_accepted_so_the_duration_expansion_can_extend_it(self):
        draft = _plan(scenes=4)
        self.assertLess(len(draft["script"].split()), 120)
        import os
        import tempfile

        original = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                with mock.patch.object(main, "GROQ_KEYS", []), \
                        mock.patch.object(main, "OPENROUTER_KEYS", []), \
                        mock.patch.object(main, "GEMINI_KEYS", []), \
                        mock.patch.object(main, "DEEPSEEK_KEYS", []), \
                        mock.patch.object(main, "CLOUDFLARE_CREDENTIALS", [{"token": "t", "account_id": "a"}]), \
                        mock.patch.object(main, "call_cloudflare_chat", return_value=draft):
                    result = main.gemini_plan("Ant colony life cycle", research_brief="Ants live in colonies.")
                written = os.path.isfile(os.path.join(temp_dir, "plan.json"))
            finally:
                os.chdir(original)
        self.assertEqual(len(result["scenes"]), 4)
        self.assertTrue(written)


class DurationExtensionTests(unittest.TestCase):
    def test_extension_asks_again_when_the_estimate_is_missed(self):
        short = _plan(scenes=4)
        deeper = dict(short, scenes=[dict(scene) for scene in short["scenes"]])
        deeper_sentences = [
            f"Chamber {index} holds larvae while nurses carry food through the "
            "tunnel and the queen lays more eggs in the warm dark, while guards "
            "patrol the entrance and keep the whole colony safe."
            for index in range(4)
        ]
        for scene, sentence in zip(deeper["scenes"], deeper_sentences):
            scene["narration"] = sentence
        deeper["script"] = " ".join(deeper_sentences)
        with mock.patch.object(main, "call_json_with_providers",
                               side_effect=[(short, "mock"), (deeper, "mock")]) as caller:
            result = main.extend_plan_for_duration(
                dict(short, scenes=[dict(scene) for scene in short["scenes"]]),
                "brief",
                target_words=120,
            )
        self.assertEqual(caller.call_count, 2)
        self.assertIn("Chamber 0", result["script"])

    def test_extension_never_fails_on_length_and_keeps_the_longest_draft(self):
        # MM7.5: no word floor exists, so a provider that keeps answering short
        # must not abort the run; the orchestrator measures the next voice-over.
        short = _plan(scenes=4)
        with mock.patch.object(main, "call_json_with_providers",
                               side_effect=[(short, "mock")] * 3):
            result = main.extend_plan_for_duration(
                dict(short, scenes=[dict(scene) for scene in short["scenes"]]),
                "brief",
                target_words=400,
            )
        self.assertTrue(result["script"])
        self.assertTrue(result["cta"]["text"])

    def test_extension_accepts_a_plan_of_any_length(self):
        short = _plan(scenes=4)
        long_plan = dict(short, scenes=[dict(scene) for scene in short["scenes"]])
        long_sentences = [
            f"Chamber {index} keeps the brood dry while workers carry larvae "
            "between rooms and the queen lays another batch of eggs."
            for index in range(4)
        ]
        for scene, sentence in zip(long_plan["scenes"], long_sentences):
            scene["narration"] = sentence
        long_plan["script"] = " ".join(long_sentences)
        with mock.patch.object(main, "call_json_with_providers",
                               return_value=(long_plan, "mock")):
            result = main.extend_plan_for_duration(
                dict(short, scenes=[dict(scene) for scene in short["scenes"]]),
                "brief",
                target_words=150,
            )
        self.assertIn("Chamber 0", result["script"])
        self.assertTrue(result["cta"]["text"])


class RepairLengthGraceTests(unittest.TestCase):
    def test_short_but_valid_plan_is_accepted(self):
        plan = _plan(scenes=4)
        self.assertLess(len(plan["script"].split()), 120)
        accepted = main.validate_plan_candidate(dict(plan))
        self.assertTrue(accepted["scenes"])
        self.assertTrue(main.script_cta.ends_with_cta(accepted["script"]))


if __name__ == "__main__":
    unittest.main()
