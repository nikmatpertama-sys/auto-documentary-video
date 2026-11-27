import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))
import main


class CoverrVerticalSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_dir = self.root / ".api-cache"
        self.state_file = self.root / "footage_source_state.json"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.patchers = [
            patch.object(main, "CACHE", self.cache_dir),
            patch.object(main, "ROOT_FOOTAGE_SOURCE_STATE_FILE", self.state_file),
            patch.object(main, "FOOTAGE_SOURCE_COOLDOWN_UNTIL", {}),
            patch.object(main, "FOOTAGE_SOURCE_REQUEST_COUNTS", {}),
            patch.object(main, "FOOTAGE_SOURCE_LAST_REQUEST_AT", {}),
            patch.object(main, "FOOTAGE_SOURCE_MIN_INTERVAL", {"Coverr": 0.0}),
            patch.object(main, "FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN", {"Coverr": 18}),
            patch.object(main, "COVERR_API_KEY", "test-key-never-log"),
            patch.object(main, "ENABLE_COVERR_SOURCE", True),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def video(**overrides):
        result = {
            "id": "coverr-123",
            "slug": "rainforest-canopy-aerial",
            "title": "Rainforest Canopy Aerial",
            "description": "A real aerial view of a tropical rainforest canopy",
            "is_vertical": True,
            "is_premium": False,
            "is_ai_generated": False,
            "aspect_ratio": "9:16",
            "max_width": 1080,
            "max_height": 1920,
            "duration": 12.4,
            "tags": ["rainforest", "canopy", "aerial"],
            "creator": {"name": "Jane Creator", "profile_url": "https://coverr.co/creators/jane"},
            "urls": {
                "mp4": "https://storage.coverr.co/videos/coverr-123?token=abc",
                "mp4_download": "https://storage.coverr.co/videos/coverr-123/download?token=abc",
            },
        }
        result.update(overrides)
        return result

    def response(self, hits, status_code=200, headers=None):
        response = Mock()
        response.status_code = status_code
        response.headers = headers or {}
        response.json.return_value = {"hits": hits, "total": len(hits)}
        if status_code >= 400:
            import requests
            error = requests.HTTPError(f"{status_code} test response")
            error.response = response
            response.raise_for_status.side_effect = error
        return response

    def test_search_uses_native_vertical_free_non_ai_filters_and_signed_mp4_url(self):
        response = self.response([self.video()])
        with patch.object(main, "_footage_source_get", return_value=response) as get:
            found = main.coverr_search("rainforest canopy aerial")

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "Coverr")
        self.assertEqual(found[0]["url"], "https://storage.coverr.co/videos/coverr-123/download?token=abc")
        self.assertTrue(main.is_native_portrait_916(found[0]["width"], found[0]["height"]))
        args, kwargs = get.call_args
        self.assertEqual(args[0], "Coverr")
        self.assertEqual(args[1], "https://api.coverr.co/videos")
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key-never-log")
        self.assertEqual(
            kwargs["params"]["filter"],
            "is_vertical:true,is_premium:false,is_ai_generated:false",
        )
        self.assertEqual(kwargs["params"]["urls"], "true")
        self.assertEqual(kwargs["params"]["page_size"], 20)

    def test_vertical_but_not_exact_916_ratios_are_rejected(self):
        hits = [
            self.video(id="4x5", aspect_ratio="4:5", max_width=1080, max_height=1350),
            self.video(id="2x3", aspect_ratio="2:3", max_width=1080, max_height=1620),
            self.video(id="916", aspect_ratio="9:16"),
        ]
        with patch.object(main, "_footage_source_get", return_value=self.response(hits)):
            found = main.coverr_search("forest")
        self.assertEqual([item["id"] for item in found], ["916"])

    def test_premium_and_ai_generated_candidates_are_rejected(self):
        hits = [
            self.video(id="premium", is_premium=True),
            self.video(id="ai", is_ai_generated=True),
            self.video(id="authentic"),
        ]
        with patch.object(main, "_footage_source_get", return_value=self.response(hits)):
            found = main.coverr_search("rainforest")
        self.assertEqual([item["id"] for item in found], ["authentic"])

    def test_untrusted_or_non_https_media_urls_are_rejected(self):
        hits = [
            self.video(id="http", urls={"mp4_download": "http://storage.coverr.co/x.mp4"}),
            self.video(id="evil", urls={"mp4_download": "https://evil.example/x.mp4"}),
            self.video(id="valid"),
        ]
        with patch.object(main, "_footage_source_get", return_value=self.response(hits)):
            found = main.coverr_search("rainforest")
        self.assertEqual([item["id"] for item in found], ["valid"])

    def test_missing_api_key_skips_coverr_without_network(self):
        with patch.object(main, "COVERR_API_KEY", ""), patch.object(main, "_footage_source_get") as get:
            self.assertEqual(main.coverr_search("forest"), [])
        get.assert_not_called()

    def test_search_fanout_only_calls_coverr_when_explicitly_selected(self):
        candidate = {"source": "Coverr", "id": "c1", "url": "https://storage.coverr.co/c1.mp4"}
        with patch.object(main, "coverr_search", return_value=[candidate]) as coverr, \
             patch.object(main, "internet_archive_search", return_value=[]), \
             patch.object(main, "commons_search", return_value=[]), \
             patch.object(main, "nasa_search", return_value=[]), \
             patch.object(main, "google_open_web_video_search", return_value=[]):
            main.search_all_footage_sources("primary scene query", include_coverr=False)
            self.assertEqual(coverr.call_count, 0)
            result = main.search_all_footage_sources("primary scene query", include_coverr=True)
        self.assertEqual(coverr.call_count, 1)
        self.assertTrue(any(item.get("source") == "Coverr" for item in result))

    def test_coverr_429_persists_cooldown_and_prevents_repeat_request(self):
        response = self.response([], status_code=429, headers={"Retry-After": "180"})
        with patch.object(main, "_footage_source_get", return_value=response) as get, \
             patch.object(main, "internet_archive_search", return_value=[]), \
             patch.object(main, "commons_search", return_value=[]), \
             patch.object(main, "nasa_search", return_value=[]), \
             patch.object(main, "web_page_video_search", return_value=[]), \
             patch.object(main, "google_open_web_video_search", return_value=[]):
            main.search_all_footage_sources("vertical rainforest", include_coverr=True)
            main.search_all_footage_sources("vertical rainforest", include_coverr=True)
        self.assertEqual(get.call_count, 1)
        self.assertGreater(main.footage_source_cooldown_remaining("Coverr"), 100)
        self.assertTrue(self.state_file.exists())

    def test_demo_api_attribution_is_included_in_generated_coverr_credit(self):
        # Validate the fields used by the credit formatter and its license reminder.
        with patch.object(main, "COVERR_API_KEY", "test-key"):
            with patch.object(main, "_footage_source_get", return_value=self.response([self.video()])):
                hit = main.coverr_search("rainforest")[0]
        self.assertIn("commercial use permitted", hit["license"])
        self.assertIn("attribution required", hit["license"])
        self.assertEqual(hit["creator"], "Jane Creator")
        self.assertEqual(hit["creator_url"], "https://coverr.co/creators/jane")


if __name__ == "__main__":
    unittest.main()
