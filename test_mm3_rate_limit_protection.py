import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))
import main


class MM3RateLimitProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_dir = self.root / ".api-cache"
        self.state_file = self.root / "footage_source_state.json"
        self.patchers = [
            patch.object(main, "CACHE", self.cache_dir),
            patch.object(main, "ROOT_FOOTAGE_SOURCE_STATE_FILE", self.state_file),
            patch.object(main, "FOOTAGE_SOURCE_COOLDOWN_UNTIL", {}),
            patch.object(main, "FOOTAGE_SOURCE_REQUEST_COUNTS", {}),
            patch.object(main, "FOOTAGE_SOURCE_LAST_REQUEST_AT", {}),
            patch.object(main, "FOOTAGE_SOURCE_MIN_INTERVAL", {name: 0.0 for name in main.FOOTAGE_SOURCE_PRIORITY}),
            patch.object(main, "FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN", {name: 100 for name in main.FOOTAGE_SOURCE_PRIORITY}),
        ]
        for item in self.patchers:
            item.start()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp_dir.cleanup()

    def test_429_cooldown_is_saved_and_can_be_rehydrated_after_process_restart(self):
        response = Mock(status_code=429, headers={"Retry-After": "120"})
        delay = main.mark_footage_source_rate_limited("Wikimedia Commons", response)
        self.assertGreaterEqual(delay, 119)

        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["sources"]["Wikimedia Commons"]["last_status"], 429)
        self.assertEqual(saved["sources"]["Wikimedia Commons"]["reason"], "http_429")

        # Simulate a fresh Python process: memory timers are gone, only the
        # tracked state file remains to restore the source cooldown.
        main.FOOTAGE_SOURCE_COOLDOWN_UNTIL.clear()
        self.assertGreater(main.footage_source_cooldown_remaining("Wikimedia Commons"), 100)


    def test_cache_only_cooldown_state_is_migrated_to_root_file_for_failed_run_persistence(self):
        cache_payload = {
            "schema_version": 1,
            "updated_at": __import__("time").time(),
            "sources": {
                "NASA": {
                    "cooldown_until": __import__("time").time() + 180,
                    "updated_at": __import__("time").time(),
                    "last_status": 429,
                    "reason": "http_429",
                }
            },
        }
        (self.cache_dir / "footage_source_state.json").write_text(
            json.dumps(cache_payload), encoding="utf-8"
        )
        main._restore_footage_source_cooldowns()
        self.assertTrue(self.state_file.exists())
        persisted = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertIn("NASA", persisted["sources"])

    def test_long_retry_after_is_not_truncated_to_a_short_retry_window(self):
        response = Mock(headers={"Retry-After": "3600"})
        self.assertEqual(main.response_retry_after_seconds(response), 3600.0)

    def test_successful_source_results_are_cached_independently_after_another_source_429(self):
        import requests
        rate_response = Mock(status_code=429, headers={"Retry-After": "180"})
        rate_error = requests.HTTPError("429 Too Many Requests")
        rate_error.response = rate_response

        with patch.object(main, "internet_archive_search", return_value=[{"source": "Internet Archive", "id": "ia-1", "url": "https://archive.org/ia-1.mp4"}]) as ia, \
             patch.object(main, "commons_search", side_effect=rate_error) as commons, \
             patch.object(main, "nasa_search", return_value=[{"source": "NASA", "id": "nasa-1", "url": "https://images.nasa.gov/nasa-1.mp4"}]) as nasa, \
             patch.object(main, "google_open_web_video_search", return_value=[]) as google:
            first = main.search_all_footage_sources("Amazon rainforest canopy aerial")
            second = main.search_all_footage_sources("Amazon rainforest canopy aerial")

        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(ia.call_count, 1)
        self.assertEqual(commons.call_count, 1)
        self.assertEqual(nasa.call_count, 1)
        self.assertEqual(google.call_count, 1)
        self.assertGreater(main.footage_source_cooldown_remaining("Wikimedia Commons"), 100)
        self.assertGreaterEqual(len(list(self.cache_dir.glob("source-*.json"))), 3)

    def test_stale_source_cache_is_used_while_provider_is_in_cooldown(self):
        old_candidates = [{
            "source": "Internet Archive",
            "id": "cached-ia",
            "url": "https://archive.org/cached-ia.mp4",
        }]
        main._write_footage_source_cache("Internet Archive", "rainforest canopy aerial", old_candidates)
        cache_path = main._footage_source_cache_path("Internet Archive", "rainforest canopy aerial")
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["cached_at"] = __import__("time").time() - 2 * 86400
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        main.mark_footage_source_temporarily_unavailable("Internet Archive", 300)

        with patch.object(main, "internet_archive_search") as ia, \
             patch.object(main, "commons_search", return_value=[]), \
             patch.object(main, "nasa_search", return_value=[]), \
             patch.object(main, "google_open_web_video_search", return_value=[]):
            found = main.search_all_footage_sources("rainforest canopy aerial")

        ia.assert_not_called()
        self.assertEqual(found[0]["id"], "cached-ia")

    def test_nasa_manifest_is_cached_and_not_downloaded_twice(self):
        response = Mock(status_code=200)
        response.json.return_value = {"collection": {"items": [{"href": "https://images.nasa.gov/video.mp4"}]}}
        with patch.object(main.requests, "get", return_value=response) as get:
            first = main._get_nasa_asset_manifest("NASA-123")
            second = main._get_nasa_asset_manifest("NASA-123")
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)


    def test_provider_5xx_is_not_cached_as_a_successful_empty_search(self):
        import requests
        response = Mock(status_code=503, headers={})
        error = requests.HTTPError("503 Service Unavailable")
        error.response = response
        # Every source is mocked: the live web-search request this test used to
        # make took over a minute when it was rate limited, and the cooldown
        # assertion below then measured an almost expired cooldown.
        with patch.object(main, "internet_archive_search", side_effect=error), \
             patch.object(main, "commons_search", return_value=[]), \
             patch.object(main, "nasa_search", return_value=[]), \
             patch.object(main, "web_page_video_search", return_value=[]), \
             patch.object(main, "google_open_web_video_search", return_value=[]):
            main.search_all_footage_sources("rare rainforest query")
        cache_path = main._footage_source_cache_path("Internet Archive", "rare rainforest query")
        self.assertFalse(cache_path.exists())
        self.assertGreater(main.footage_source_cooldown_remaining("Internet Archive"), 60)

    def test_request_pacing_waits_before_the_next_request_to_same_source(self):
        response = Mock(status_code=200)
        with patch.object(main, "FOOTAGE_SOURCE_MIN_INTERVAL", {"NASA": 4.0}), \
             patch.object(main, "FOOTAGE_SOURCE_LAST_REQUEST_AT", {"NASA": __import__("time").monotonic() - 1.0}), \
             patch.object(main.time, "sleep") as sleep, \
             patch.object(main.requests, "get", return_value=response):
            main._footage_source_get("NASA", "https://images-api.nasa.gov/search")
        self.assertTrue(sleep.called)
        self.assertGreaterEqual(sleep.call_args.args[0], 2.9)

    def test_request_governor_enforces_per_run_ceiling_without_remote_retry(self):
        response = Mock(status_code=200)
        with patch.object(main, "FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN", {"NASA": 1}), \
             patch.object(main.requests, "get", return_value=response) as get:
            main._footage_source_get("NASA", "https://images-api.nasa.gov/search")
            with self.assertRaises(main.FootageSourceBudgetExceeded):
                main._footage_source_get("NASA", "https://images-api.nasa.gov/search")
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
