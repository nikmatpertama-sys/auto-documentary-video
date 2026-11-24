import math
import unittest

from narration_timing import duration_correction


class NarrationTimingTests(unittest.TestCase):
    def test_short_narration_is_gently_slowed_to_safe_target(self):
        result = duration_correction(66.92)
        self.assertAlmostEqual(result["target_duration"], 71.0)
        self.assertAlmostEqual(result["atempo"], 66.92 / 71.0)
        self.assertLess(result["atempo"], 1.0)

    def test_long_narration_is_gently_sped_up_to_safe_target(self):
        result = duration_correction(90.6)
        self.assertAlmostEqual(result["target_duration"], 89.0)
        self.assertAlmostEqual(result["atempo"], 90.6 / 89.0)
        self.assertGreater(result["atempo"], 1.0)

    def test_duration_inside_requested_range_is_unchanged(self):
        self.assertIsNone(duration_correction(70.0))
        self.assertIsNone(duration_correction(80.0))
        self.assertIsNone(duration_correction(90.0))

    def test_invalid_durations_are_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), "not-a-duration"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    duration_correction(value)

    def test_extreme_duration_is_not_force_corrected(self):
        for value in (20.0, 55.0, 110.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    duration_correction(value)

    def test_legitimate_short_script_is_stretched_instead_of_failing(self):
        # 58s of speech still needs a pitch-preserving stretch to reach 70s; the
        # floor must accept it rather than failing after TTS has been paid for.
        result = duration_correction(58.0)
        self.assertAlmostEqual(result["target_duration"], 71.0)
        self.assertGreaterEqual(result["atempo"], 0.80)


if __name__ == "__main__":
    unittest.main()
