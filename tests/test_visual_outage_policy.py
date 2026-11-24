import unittest
from visual_outage_policy import metadata_fallback_decision


class VisualOutagePolicyTests(unittest.TestCase):
    def test_strong_direct_match_is_accepted(self):
        ok, reason = metadata_fallback_decision(.60, 3, 5, .20, 1, 5)
        self.assertTrue(ok)
        self.assertIn('query metadata', reason)

    def test_strong_narration_match_can_override_camera_search_terms(self):
        ok, reason = metadata_fallback_decision(.38, 2, 5, .58, 3, 6)
        self.assertTrue(ok)
        self.assertIn('narration-context', reason)

    def test_weak_candidate_is_still_rejected(self):
        ok, reason = metadata_fallback_decision(.38, 2, 5, .30, 1, 6)
        self.assertFalse(ok)
        self.assertIn('insufficient', reason)

    def test_single_keyword_match_is_not_enough(self):
        ok, _ = metadata_fallback_decision(.70, 1, 5, .70, 1, 6)
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
