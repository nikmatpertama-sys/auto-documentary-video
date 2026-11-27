"""MM7.5: no narration word limits, a measured 70 second voice-over, and a
spoken follow + comment CTA at the very end of the narration."""

import unittest
from unittest import mock

import main
import script_cta


def build_plan(narrations, title="Ant colony life cycle"):
    scenes = [
        {
            "narration": text,
            "visual_query": "ant colony soil",
            "visual_queries": ["ant colony", "ant nest"],
            "source_label": "Ant",
            "source_url": "https://en.wikipedia.org/wiki/Ant",
        }
        for text in narrations
    ]
    return {
        "title": title,
        "hook": "What if a colony thinks?",
        "script": " ".join(narrations),
        "scenes": scenes,
    }


SHORT = [
    "Ants live in colonies.",
    "The queen lays eggs.",
    "Workers feed the brood.",
    "The colony grows.",
]


class NoWordLimitTests(unittest.TestCase):
    def test_short_script_is_accepted(self):
        plan = main.validate_plan_candidate(build_plan(SHORT))
        self.assertEqual(len(plan["scenes"]), 4)
        self.assertLess(len(plan["script"].split()), 40)

    def test_very_long_script_is_accepted(self):
        narrations = [
            f"Scene {index} shows workers carrying brood through a damp tunnel "
            "while the queen lays another batch of eggs and nurses tend the larvae "
            "in the warm upper chambers of the nest, where humidity stays steady "
            "through the night and the colony keeps expanding."
            for index in range(8)
        ]
        plan = main.validate_plan_candidate(build_plan(narrations))
        self.assertGreater(len(plan["script"].split()), 200)

    def test_there_is_no_word_constant_left(self):
        for name in ("MIN_PLAN_WORDS", "MIN_REPAIRED_WORDS", "MAX_PLAN_WORDS"):
            self.assertFalse(hasattr(main, name), name)

    def test_validator_takes_no_word_bounds(self):
        import inspect

        parameters = inspect.signature(main.validate_plan_candidate).parameters
        self.assertNotIn("min_words", parameters)
        self.assertNotIn("max_words", parameters)


class ClosingCtaTests(unittest.TestCase):
    def test_cta_is_appended_when_the_writer_omits_it(self):
        plan = main.validate_plan_candidate(build_plan(SHORT))
        self.assertEqual(plan["cta"]["source"], "appended")
        self.assertTrue(script_cta.ends_with_cta(plan["script"]))
        self.assertTrue(script_cta.is_cta_sentence(script_cta.last_sentence(plan["script"])))
        self.assertIn("ant", " ".join(scene["narration"] for scene in plan["scenes"]).lower())

    def test_a_written_cta_is_kept_verbatim(self):
        narrations = SHORT + [
            "Follow for more documentaries like this, and tell us in the comments "
            "what you would explore next."
        ]
        plan = main.validate_plan_candidate(build_plan(narrations))
        self.assertEqual(plan["cta"]["source"], "script")
        self.assertEqual(
            script_cta.last_sentence(plan["script"]), narrations[-1]
        )

    def test_only_the_final_cta_survives(self):
        narrations = [
            "Ants live in colonies.",
            "Subscribe and comment below for more.",
            "The queen lays eggs.",
            "Follow and tell us what you think in the comments.",
        ]
        plan = main.validate_plan_candidate(build_plan(narrations))
        body = " ".join(scene["narration"] for scene in plan["scenes"]).lower()
        self.assertEqual(body.count("follow"), 1)
        self.assertTrue(script_cta.ends_with_cta(plan["script"]))
        self.assertEqual(plan["cta"]["source"], "script")

    def test_cta_is_varied_between_topics(self):
        first = script_cta.build_cta("en", topic="Ant colony life cycle")
        second = script_cta.build_cta("en", topic="Volcano eruption")
        self.assertNotEqual(first, second)
        self.assertTrue(script_cta.is_cta_sentence(first))
        self.assertTrue(script_cta.is_cta_sentence(second))


class SpokenCtaTests(unittest.TestCase):
    def _plan(self):
        return main.validate_plan_candidate(
            build_plan(
                SHORT
                + [
                    "Follow for more documentaries like this, and let us know your "
                    "thoughts in the comments below."
                ]
            )
        )

    def test_cta_verified_when_it_is_the_last_spoken_sentence(self):
        plan = self._plan()
        events = [
            {"start": 0.0, "end": 30.0, "text": "Ants live in colonies."},
            {
                "start": 30.1,
                "end": 74.5,
                "text": script_cta.last_sentence(plan["script"]),
            },
        ]
        result = main.verify_cta_in_voiceover(plan, events, 74.5)
        self.assertEqual(result["end"], 74.5)

    def test_audio_without_a_final_cta_is_rejected(self):
        plan = self._plan()
        events = [
            {"start": 0.0, "end": 70.0, "text": "Ants live in colonies."},
            {"start": 70.1, "end": 71.0, "text": "The colony grows."},
        ]
        with self.assertRaisesRegex(RuntimeError, "final spoken sentence is not the CTA"):
            main.verify_cta_in_voiceover(plan, events, 71.0)

    def test_cta_buried_before_the_end_is_rejected(self):
        plan = self._plan()
        cta = script_cta.last_sentence(plan["script"])
        events = [
            {"start": 0.0, "end": 40.0, "text": cta},
            {"start": 40.1, "end": 75.0, "text": "The colony grows."},
        ]
        with self.assertRaises(RuntimeError):
            main.verify_cta_in_voiceover(plan, events, 75.0)

    def test_plan_without_cta_cannot_pass_audio_validation(self):
        plan = main.validate_plan_candidate(build_plan(SHORT))
        plan["cta"] = {"text": "", "source": "missing"}
        events = [{"start": 0.0, "end": 75.0, "text": "Ants live in colonies."}]
        with self.assertRaisesRegex(RuntimeError, "no closing CTA"):
            main.verify_cta_in_voiceover(plan, events, 75.0)

    def test_spoken_duration_excludes_the_trailing_hold(self):
        events = [{"start": 0.0, "end": 71.4, "text": "Ants live in colonies."}]
        self.assertAlmostEqual(main._spoken_duration(events, 71.7), 71.4)


class DurationFloorTests(unittest.TestCase):
    def test_voice_over_under_70_seconds_needs_more_script(self):
        self.assertEqual(main.MIN_VIDEO_DURATION, 70.0)
        self.assertTrue(main.needs_duration_extension(58.34))
        self.assertTrue(main.needs_duration_extension(69.9))

    def test_voice_over_at_or_above_70_seconds_is_accepted(self):
        self.assertFalse(main.needs_duration_extension(70.0))
        self.assertFalse(main.needs_duration_extension(118.4))


if __name__ == "__main__":
    unittest.main()
