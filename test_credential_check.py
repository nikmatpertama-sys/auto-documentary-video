import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "check_credentials", Path(__file__).with_name("tools") / "check_credentials.py"
)
check_credentials = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_credentials)


class CredentialCheckTests(unittest.TestCase):
    def test_reports_set_and_missing_without_values(self):
        env = {
            "GROQ_API_KEY": "secret-groq-value",
            "OPENROUTER_API_KEY_2": "secret-openrouter-value",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            report = check_credentials.collect_report()
        self.assertTrue(report["groups"]["groq"]["set"])
        self.assertEqual(report["groups"]["groq"]["present_names"], ["GROQ_API_KEY"])
        self.assertTrue(report["groups"]["openrouter"]["set"])
        self.assertEqual(report["groups"]["openrouter"]["present_names"], ["OPENROUTER_API_KEY_2"])
        self.assertFalse(report["groups"]["speechify"]["set"])
        self.assertIn("groq", report["text_providers_available"])
        self.assertIn("openrouter", report["text_providers_available"])
        serialized = json.dumps(report)
        self.assertNotIn("secret-groq-value", serialized)
        self.assertNotIn("secret-openrouter-value", serialized)

    def test_cloudflare_requires_a_paired_account_id(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "token-only"}, clear=True):
            report = check_credentials.collect_report()
        self.assertFalse(report["groups"]["cloudflare"]["set"])
        self.assertTrue(report["groups"]["cloudflare"]["partial_pairs"])

        with mock.patch.dict(
            os.environ,
            {"CF_API_TK1": "token", "CF_ID1": "account"},
            clear=True,
        ):
            report = check_credentials.collect_report()
        self.assertTrue(report["groups"]["cloudflare"]["set"])
        self.assertEqual(report["groups"]["cloudflare"]["present_names"], ["CF_API_TK1"])

    def test_google_api_key_alias_counts_as_a_gemini_credential(self):
        with mock.patch.dict(os.environ, {"GOOGLE_API_KEY": "alias-key"}, clear=True):
            report = check_credentials.collect_report()
        self.assertTrue(report["groups"]["gemini"]["set"])
        self.assertEqual(report["groups"]["gemini"]["present_names"], ["GOOGLE_API_KEY"])
        self.assertIn("gemini", report["vision_providers_available"])

    def test_exit_code_fails_closed_without_any_text_provider(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(check_credentials.main([]), 1)

    def test_json_report_contains_names_and_booleans_only(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-secret-value"}, clear=True):
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "report.json"
                self.assertEqual(check_credentials.main(["--json-out", str(target)]), 0)
                payload = target.read_text(encoding="utf-8")
        self.assertIn("GEMINI_API_KEY", payload)
        self.assertNotIn("gemini-secret-value", payload)


if __name__ == "__main__":
    unittest.main()
