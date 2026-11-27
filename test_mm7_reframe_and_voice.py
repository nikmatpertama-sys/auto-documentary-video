"""Tests for MM7 reframing of wide scenes and the Speechify voice chain.

Production failed with "Required visual content is too wide for 9:16" on every
candidate for an insect-trail scene. A landscape window of a 16:9 source is
31.6% of its width, so any shot whose subject is the whole expanse (aerial
canopy, horizon, trail, crowd) could never pass. These tests pin the rule that
replaced it: discrete subjects must still fit one window, extended scenes may
be traversed by a smooth pan, and the pan is still bounded.
"""

import unittest

import main


SOURCE = {"width": 1920, "height": 1080, "aspect_ratio": 16 / 9}
CROP_FRACTION = main.TARGET_SOURCE_ASPECT / (16 / 9)


def _payload(boxes, kind=None, fits=True):
    payload = {
        "subject": "canopy",
        "subject_visible": True,
        "composition_fits": fits,
        "reason": "test",
        "frames": [{"bbox": box, "confidence": 0.9} for box in boxes],
    }
    if kind is not None:
        payload["subject_kind"] = kind
    return payload


class ExtendedSceneCropTests(unittest.TestCase):
    def test_wide_extended_scene_is_reframed_instead_of_rejected(self):
        plan = main.validate_smart_crop_plan(
            _payload([[0.05, 0.10, 0.95, 0.92]] * 4, kind="extended"),
            SOURCE,
            [0.0, 0.5, 1.0, 1.5],
        )
        self.assertEqual(plan["subject_kind"], "extended")
        self.assertGreater(plan["extended_scene_frames"], 0)
        self.assertEqual(len(plan["points"]), 4)
        for point in plan["points"]:
            # Window stays inside the extended content.
            self.assertGreaterEqual(point["x"], 0.05 - 1e-6)
            self.assertLessEqual(point["x"] + CROP_FRACTION, 0.95 + 1e-6)

    def test_wide_discrete_subject_is_still_rejected(self):
        with self.assertRaises(RuntimeError) as caught:
            main.validate_smart_crop_plan(
                _payload([[0.00, 0.20, 0.80, 0.90]] * 4, kind="discrete"),
                SOURCE,
                [0.0, 0.5, 1.0, 1.5],
            )
        self.assertIn("too wide for 9:16", str(caught.exception))

    def test_unclassified_full_frame_scene_is_treated_as_extended(self):
        plan = main.validate_smart_crop_plan(
            _payload([[0.0, 0.05, 0.98, 0.95]] * 4),
            SOURCE,
            [0.0, 0.5, 1.0, 1.5],
        )
        self.assertEqual(plan["subject_kind"], "extended")

    def test_narrow_subject_keeps_margin_containment(self):
        plan = main.validate_smart_crop_plan(
            _payload([[0.30, 0.10, 0.42, 0.90]] * 4),
            SOURCE,
            [0.0, 0.5, 1.0, 1.5],
        )
        self.assertEqual(plan["subject_kind"], "discrete")
        self.assertEqual(plan["extended_scene_frames"], 0)

    def test_extended_scene_still_rejects_an_unsafe_pan(self):
        boxes = [
            [0.00, 0.10, 0.40, 0.90],
            [0.00, 0.10, 0.40, 0.90],
            [0.60, 0.10, 1.00, 0.90],
            [0.60, 0.10, 1.00, 0.90],
        ]
        with self.assertRaises(RuntimeError) as caught:
            main.validate_smart_crop_plan(
                _payload(boxes, kind="extended"), SOURCE, [0.0, 0.5, 1.0, 1.5]
            )
        self.assertIn("abruptly", str(caught.exception))

    def test_missing_composition_fit_is_still_fail_closed(self):
        with self.assertRaises(RuntimeError):
            main.validate_smart_crop_plan(
                _payload([[0.0, 0.0, 1.0, 1.0]] * 4, kind="extended", fits=False),
                SOURCE,
                [0.0, 0.5, 1.0, 1.5],
            )


class SpeechifyVoiceChainTests(unittest.TestCase):
    def test_documented_voice_is_the_fallback_when_catalog_is_unreadable(self):
        with unittest.mock.patch.object(main, "SPEECHIFY_VOICE_ID", ""):
            with unittest.mock.patch.object(main, "_speechify_choose_voice", return_value=""):
                candidates = main._speechify_voice_candidates(["key"])
        self.assertEqual(candidates, [main.SPEECHIFY_DOCUMENTED_VOICE_ID])

    def test_pinned_voice_is_tried_first(self):
        with unittest.mock.patch.object(main, "SPEECHIFY_VOICE_ID", "pinned_voice"):
            with unittest.mock.patch.object(main, "_speechify_choose_voice", return_value="catalog_voice"):
                candidates = main._speechify_voice_candidates(["key"])
        self.assertEqual(candidates[0], "pinned_voice")
        self.assertEqual(candidates[-1], main.SPEECHIFY_DOCUMENTED_VOICE_ID)

    def test_only_voice_rejections_rotate_the_voice(self):
        self.assertTrue(main._speechify_invalid_voice_error(RuntimeError("Speechify key 1 HTTP 404: voice not found")))
        self.assertFalse(main._speechify_invalid_voice_error(RuntimeError("Speechify key 1 HTTP 402: payment")))
        self.assertFalse(main._speechify_invalid_voice_error(RuntimeError("Speechify key 1 HTTP 429: slow down")))


if __name__ == "__main__":
    unittest.main()
