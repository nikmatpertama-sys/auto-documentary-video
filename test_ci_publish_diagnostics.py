"""Tests for tools/ci_publish_diagnostics.py.

The diagnostics step must never abort on a missing artifact and must never
publish a credential. Both were real failures: a run's report never reached the
branch, and the only place left to read it was an Actions log that needs account
access.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

import ci_publish_diagnostics as diag  # noqa: E402


class DiagnosticsPublishTests(unittest.TestCase):
    def test_annotations_are_single_line_and_publicly_readable(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            diag.annotate("error", "Pipeline stage: footage", "line one\nline two")
        printed = buffer.getvalue()
        self.assertIn("::error title=Pipeline stage: footage::line one line two", printed)

    def test_bearer_and_key_shapes_are_redacted(self):
        text = diag.redacted("Authorization: Bearer sk-abcdef12345678 and AIzaSyABCDEFGHIJKL")
        self.assertNotIn("sk-abcdef12345678", text)
        self.assertNotIn("AIzaSyABCDEFGHIJKL", text)
        self.assertIn("[redacted]", text)

    def test_failure_annotation_reports_stage_and_redacts_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "failure_report.json").write_text(json.dumps({
                "current_stage": "voice_over",
                "error_type": "RuntimeError",
                "error_message": "Speechify key sk-livekey1234567 rejected",
            }))
            buffer = io.StringIO()
            previous = os.getcwd()
            os.chdir(root)
            try:
                with redirect_stdout(buffer):
                    diag.report_failure()
            finally:
                os.chdir(previous)
        printed = buffer.getvalue()
        self.assertIn("Pipeline stage: voice_over", printed)
        self.assertNotIn("sk-livekey1234567", printed)

    def test_missing_artifacts_do_not_raise(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            previous = os.getcwd()
            os.chdir(root)
            try:
                with redirect_stdout(io.StringIO()):
                    diag.report_failure()
                    copied = diag.copy_artifacts(root / "latest")
                    captured = diag.capture_video(root / "latest")
            finally:
                os.chdir(previous)
        self.assertEqual(copied, [])
        self.assertEqual(captured, [])

    def test_pipeline_log_is_published_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "output").mkdir()
            (root / "output" / "pipeline.log").write_text(
                "start\nAuthorization: Bearer sk-secretvalue1234\nend\n"
            )
            (root / "timing.json").write_text("{}")
            previous = os.getcwd()
            os.chdir(root)
            try:
                copied = diag.copy_artifacts(root / "latest")
                published = (root / "latest" / "output" / "pipeline.log").read_text()
            finally:
                os.chdir(previous)
        self.assertIn("output/pipeline.log", copied)
        self.assertIn("timing.json", copied)
        self.assertNotIn("sk-secretvalue1234", published)

    def test_publish_retries_a_rejected_push(self):
        push_results = [mock.Mock(returncode=1, stderr="rejected", stdout=""), mock.Mock(returncode=0, stderr="", stdout="")]
        with mock.patch.object(diag, "run", side_effect=[mock.Mock(returncode=0, stderr="", stdout="")] * 1 + push_results + [mock.Mock(returncode=0, stderr="", stdout="")]) as runner:
            with mock.patch.object(diag.time, "sleep"):
                with redirect_stdout(io.StringIO()):
                    diag.publish(Path(tempfile.mkdtemp()))
        self.assertTrue(any("push" in " ".join(call.args[0]) for call in runner.call_args_list))


if __name__ == "__main__":
    unittest.main()
