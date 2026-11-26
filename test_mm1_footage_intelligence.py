import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# main.py imports edge_tts for the narration path; footage logic tests do not
# invoke it, so keep this suite independent from an installed TTS package.
sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

import main


class MM1RelevanceTests(unittest.TestCase):
    def setUp(self):
        self.query = "Amazon rainforest canopy aerial drone view"
        self.narration = "Dense tropical rainforest canopy stretches across the Amazon basin."

    def test_military_jungle_training_is_flagged_as_unrequested_context(self):
        hit = {
            "title": "Military jungle training exercise",
            "tags": "soldiers army military training tropical forest",
            "metadata": "Troops practicing tactical movement through jungle terrain",
        }
        conflicts = main.unrequested_context_conflicts(hit, self.query, self.narration)
        self.assertIn("military", conflicts)

    def test_real_canopy_metadata_scores_above_incidental_description_mention(self):
        strong = {
            "title": "Amazon rainforest canopy drone aerial",
            "tags": "Amazon tropical rainforest canopy drone aerial treetops",
            "metadata": "A wide overhead shot of continuous rainforest canopy",
        }
        weak = {
            "title": "Archive reel 1934",
            "tags": "forest video archive",
            "metadata": (
                "A historical film. The description mentions Amazon rainforest "
                "canopy aerial drone footage as one of many unrelated catalog terms."
            ),
        }
        strong_score = main.relevance_score(strong, self.query, self.narration)[0]
        weak_score = main.relevance_score(weak, self.query, self.narration)[0]
        self.assertGreater(strong_score, weak_score + 0.08)

    def test_generic_forest_is_not_a_strict_rainforest_or_aerial_match(self):
        hit = {
            "title": "Dense forest trees",
            "metadata": "Tall trees in a dense forest",
            "tags": "trees forest vegetation",
        }
        self.assertFalse(
            main.candidate_matches_visual_anchors(hit, self.query, primary_query=self.query)
        )
        # A generic forest/tree description lacks the requested aerial shot
        # evidence and should not enter even the relaxed search queue here.
        self.assertFalse(
            main.candidate_matches_relaxed_metadata(
                hit, self.query, self.narration
            )
        )

    def test_relevant_candidate_can_beat_a_higher_priority_source_when_score_is_much_better(self):
        archive = {"source": "Internet Archive", "id": "archive"}
        google = {"source": "Google Search", "id": "google"}
        ranked = main.rank_footage_candidates_by_source([
            (0.62, archive, "q", 2, 3),
            (0.91, google, "q", 3, 3),
        ])
        self.assertEqual(ranked[0][1]["id"], "google")

    def test_open_archive_breaks_a_close_relevance_tie(self):
        archive = {"source": "Internet Archive", "id": "archive"}
        google = {"source": "Google Search", "id": "google"}
        ranked = main.rank_footage_candidates_by_source([
            (0.84, google, "q", 3, 3),
            (0.87, archive, "q", 3, 3),
        ])
        self.assertEqual(ranked[0][1]["id"], "archive")

    def test_wikimedia_search_429_cools_commons_and_other_sources_continue(self):
        import requests
        from unittest.mock import Mock

        error_response = Mock()
        error_response.status_code = 429
        error_response.headers = {"Retry-After": "60"}
        rate_limit = requests.HTTPError("HTTP 429 Too Many Requests")
        rate_limit.response = error_response

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / ".api-cache"
            with patch.object(main, "CACHE", cache_dir), \
                 patch.object(main, "FOOTAGE_SOURCE_COOLDOWN_UNTIL", {}), \
                 patch.object(main, "internet_archive_search", return_value=[]), \
                 patch.object(main, "commons_search", side_effect=rate_limit) as commons, \
                 patch.object(main, "nasa_search", return_value=[{"source": "NASA"}]) as nasa, \
                 patch.object(main, "google_open_web_video_search", return_value=[]):
                first = main.search_all_footage_sources("Amazon rainforest canopy aerial")
                second = main.search_all_footage_sources("Amazon rainforest canopy aerial")

                self.assertEqual(first, [{"source": "NASA"}])
                self.assertEqual(second, [{"source": "NASA"}])
                self.assertEqual(commons.call_count, 1)
                self.assertEqual(nasa.call_count, 2)
                self.assertGreater(
                    main.FOOTAGE_SOURCE_COOLDOWN_UNTIL["Wikimedia Commons"],
                    __import__("time").monotonic(),
                )
                self.assertFalse(list(cache_dir.glob("*.sources.json")))

    def test_wikimedia_429_is_not_retried_and_leaves_no_partial_file(self):
        import requests
        from unittest.mock import Mock

        class RateLimitedResponse:
            status_code = 429
            headers = {"Retry-After": "60"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def raise_for_status(self):
                error = requests.HTTPError("HTTP 429 Too Many Requests")
                error.response = self
                raise error

        response = RateLimitedResponse()
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "clip.mp4"
            with patch.object(main.requests, "get", return_value=response) as get, \
                 patch.object(main, "mark_footage_source_rate_limited") as cooldown:
                with self.assertRaises(requests.HTTPError):
                    main.download_stream(
                        "https://upload.wikimedia.org/example/clip.mp4", target
                    )

            self.assertEqual(get.call_count, 1)
            cooldown.assert_called_once_with("Wikimedia Commons", response)
            self.assertFalse(target.exists())

    def test_retry_query_is_specific_and_bounded(self):
        scene = {
            "visual_query": self.query,
            "narration": self.narration,
        }
        feedback = [{
            "reason": "The clip shows a ground-level path rather than a broad overhead canopy.",
        }]
        queries = main.adaptive_visual_retry_queries(scene, feedback)
        self.assertGreaterEqual(len(queries), 2)
        self.assertLessEqual(len(queries), main.MAX_ADAPTIVE_FOOTAGE_QUERIES)
        self.assertTrue(any("overhead" in value or "altitude" in value for value in queries))
        for value in queries:
            self.assertTrue(
                main.candidate_matches_visual_anchors(
                    {
                        "title": "Amazon rainforest canopy drone aerial",
                        "metadata": "Amazon rainforest canopy drone aerial",
                        "tags": "Amazon rainforest canopy drone aerial",
                    },
                    value,
                    primary_query=self.query,
                )
            )


class MM1AdaptiveRetryIntegrationTests(unittest.TestCase):
    def test_visual_rejection_triggers_a_second_search_and_accepts_new_candidate(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scene = {
                "visual_query": "Amazon rainforest canopy aerial drone shot",
                "narration": "Dense rainforest canopy spreads across the Amazon basin.",
            }
            bad = {
                "source": "Internet Archive",
                "id": "bad-ground-level",
                "url": "https://example.invalid/bad.mp4",
                "title": "Amazon rainforest canopy drone aerial ground trail",
                "metadata": "Amazon rainforest canopy drone aerial ground trail",
                "tags": "Amazon rainforest canopy drone aerial ground trail",
                "width": 720,
                "height": 1280,
                "duration": 12,
                "page_url": "https://archive.org/details/bad-ground-level",
                "creator": "Test",
                "license": "CC0",
            }
            good = {
                "source": "Internet Archive",
                "id": "good-overhead",
                "url": "https://example.invalid/good.mp4",
                "title": "Amazon rainforest canopy drone overhead aerial",
                "metadata": "Amazon rainforest canopy drone overhead aerial wide view",
                "tags": "Amazon rainforest canopy drone overhead aerial wide view",
                "width": 720,
                "height": 1280,
                "duration": 12,
                "page_url": "https://archive.org/details/good-overhead",
                "creator": "Test",
                "license": "CC0",
            }
            searched_queries = []
            visual_calls = []

            def fake_search(query):
                searched_queries.append(query)
                if "altitude" in query.lower():
                    return [dict(good)]
                return [dict(bad)]

            def fake_download(url, dest, source=None):
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(b"x" * 60000)

            def fake_verify(video_path, scene_arg, query, metadata):
                visual_calls.append((query, metadata))
                if "ground trail" in metadata.lower():
                    return (
                        False,
                        0.41,
                        "The visible shot is a ground-level path, not broad overhead canopy.",
                        "TestVision",
                    )
                return (
                    True,
                    0.93,
                    "The frames show a broad Amazon rainforest canopy from overhead.",
                    "TestVision",
                )

            # The first item is visually rejected. The adaptive query includes
            # overhead/high-altitude terms and returns a distinct verified clip.
            cwd_was_changed = False
            try:
                os.chdir(root)
                cwd_was_changed = True
                with patch.object(main, "OUT", root / "output"), \
                     patch.object(main, "FOOT", root / "output" / "footage"), \
                     patch.object(main, "CACHE", root / ".api-cache"), \
                     patch.object(main, "GROQ_KEYS", ["unit-test-key"]), \
                     patch.object(main, "VISION_REQUIRED", True), \
                     patch.object(main, "search_all_footage_sources", side_effect=fake_search), \
                     patch.object(main, "download_stream", side_effect=fake_download), \
                     patch.object(main, "ffprobe_video_info", return_value={
                         "width": 720, "height": 1280, "aspect_ratio": 9 / 16,
                     }), \
                     patch.object(main, "verify_footage_visually", side_effect=fake_verify):
                    plan = {"scenes": [scene]}
                    main.fetch_footage(plan)
            finally:
                if cwd_was_changed:
                    os.chdir(original_cwd)

            self.assertEqual(scene["selected_footage_source"], "Internet Archive")
            self.assertIn("good-overhead", scene["selected_footage_url"])
            self.assertGreaterEqual(scene["footage_search_retry_count"], 2)
            self.assertGreaterEqual(len(visual_calls), 2)
            self.assertTrue(
                any("altitude" in query.lower()
                    for query in searched_queries)
            )


if __name__ == "__main__":
    unittest.main()
