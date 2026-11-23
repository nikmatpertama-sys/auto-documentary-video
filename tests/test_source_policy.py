import unittest

from source_policy import source_failure_reason


class SourcePolicyTests(unittest.TestCase):
    def test_auth_permission_and_rate_limit_errors_disable_provider(self):
        self.assertEqual(source_failure_reason(401), "unauthorized")
        self.assertEqual(source_failure_reason(403), "forbidden")
        self.assertEqual(source_failure_reason(429), "rate-limited")

    def test_transient_and_success_statuses_do_not_disable_provider(self):
        for status in (200, 400, 404, 500, 502, 503, None, "unknown"):
            with self.subTest(status=status):
                self.assertIsNone(source_failure_reason(status))


if __name__ == "__main__":
    unittest.main()
