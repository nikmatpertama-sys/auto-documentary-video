"""Tests for tools/speechify_tts_probe.py.

These assert that the probe measures production behaviour: its request body is
built from main.py's own SSML helper, and no credential value can reach the
report or the console output.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

import main as pipeline  # noqa: E402
import speechify_tts_probe as probe  # noqa: E402


class SpeechifyProbeTests(unittest.TestCase):
    def test_ssml_matches_the_production_helper(self):
        sentence = "The colony survives because every worker shares the same task."
        self.assertEqual(
            probe.production_ssml(sentence, 3, 10),
            pipeline._speechify_sentence_ssml(sentence, 3, 10),
        )
        self.assertEqual(
            probe.production_ssml("What happens at dusk?", 1, 4),
            pipeline._speechify_sentence_ssml("What happens at dusk?", 1, 4),
        )

    def test_body_uses_the_production_fields(self):
        body = probe.production_body("geffen_32")
        self.assertEqual(set(body), {"input", "voice_id", "audio_format", "language", "model"})
        self.assertEqual(body["voice_id"], "geffen_32")
        self.assertEqual(body["audio_format"], "wav")
        self.assertTrue(body["input"].startswith("<speak><prosody"))

    def test_pinned_voice_wins_and_ids_are_deduplicated(self):
        voices = [
            {"id": "calm_deep_1", "tags": ["calm", "deep"]},
            {"id": "geffen_32", "tags": []},
        ]
        with mock.patch.dict(os.environ, {"SPEECHIFY_VOICE_ID": "pinned_voice"}, clear=False):
            candidates = probe.candidate_voices(voices)
        ids = [item["voice_id"] for item in candidates]
        self.assertEqual(ids[0], "pinned_voice")
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("geffen_32", ids)

    def test_report_never_contains_a_credential_value(self):
        secret = "sk_test_secret_value_should_never_appear"
        with mock.patch.dict(os.environ, {"SPVO_API": secret}, clear=False):
            with mock.patch.object(probe, "probe_catalog", return_value={"env": "SPVO_API", "variants": [], "catalog_voices": []}):
                with mock.patch.object(
                    probe,
                    "probe_speech",
                    return_value=[
                        {
                            "env": "SPVO_API",
                            "voice_id": "geffen_32",
                            "voice_source": "documented quickstart example",
                            "http_status": 403,
                            "error_code": "forbidden",
                            "audio_ok": False,
                        }
                    ],
                ):
                    report = probe.run(tempfile.mkdtemp())
        serialised = json.dumps(report)
        self.assertNotIn(secret, serialised)
        self.assertIn("SPVO_API", serialised)
        self.assertFalse(report["tts_audio_produced"])

    def test_audio_measurement_rejects_silence(self):
        import wave

        silent = Path(tempfile.mkdtemp()) / "silence.wav"
        with wave.open(str(silent), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(b"\x00\x00" * 24000)
        measurement = probe._measure_wav(str(silent))
        self.assertFalse(measurement["valid"])
        self.assertTrue(measurement["silent"])

        audible = Path(tempfile.mkdtemp()) / "tone.wav"
        with wave.open(str(audible), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(b"\x10\x20" * 24000)
        self.assertTrue(probe._measure_wav(str(audible))["valid"])

    def test_error_code_extraction_is_stable(self):
        payload = json.dumps({"error": {"code": "forbidden", "message": "nope"}}).encode()
        self.assertEqual(probe._error_code(payload), "forbidden")
        self.assertEqual(probe._error_code(b"not json"), "")


if __name__ == "__main__":
    unittest.main()
