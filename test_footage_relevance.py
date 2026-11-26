import sys
import types
import unittest

# main.py imports edge_tts for the rendering path; this test exercises only
# pure metadata matching and does not need the speech package.
sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from main import (
    candidate_matches_visual_anchors,
    candidate_matches_relaxed_metadata,
)


class RainforestMetadataFallbackTests(unittest.TestCase):
    def test_tree_closeup_metadata_can_match_amazon_tree_scene(self):
        hit = {
            "metadata": "Tall tropical trees and forest tree trunk close up",
            "tags": "trees, forest, vegetation",
            "page_url": "https://example.invalid/forest-clip",
        }
        self.assertTrue(
            candidate_matches_visual_anchors(
                hit, "tall tropical rainforest trees close up",
                primary_query="tall amazon trees close up",
            )
        )


    def test_generic_forest_metadata_enters_visual_ai_fallback_queue(self):
        hit = {
            "metadata": "Tall trees in a dense forest",
            "tags": "trees, forest, vegetation",
            "page_url": "https://example.invalid/trees",
        }
        self.assertTrue(
            candidate_matches_relaxed_metadata(
                hit,
                "tall amazon rainforest canopy aerial",
                "Trees tower above the rainforest",
            )
        )

    def test_unrelated_city_metadata_stays_out_of_relaxed_queue(self):
        hit = {
            "metadata": "City buildings and urban traffic",
            "tags": "city, buildings, traffic",
            "page_url": "https://example.invalid/city",
        }
        self.assertFalse(
            candidate_matches_relaxed_metadata(
                hit,
                "tall amazon rainforest canopy aerial",
                "Trees tower above the rainforest",
            )
        )

    def test_unrelated_urban_metadata_stays_rejected(self):
        hit = {
            "metadata": "Tall city buildings and urban street traffic",
            "tags": "city, buildings, traffic",
            "page_url": "https://example.invalid/city-clip",
        }
        self.assertFalse(
            candidate_matches_visual_anchors(
                hit, "tall tropical rainforest trees close up",
                primary_query="tall amazon trees close up",
            )
        )


if __name__ == "__main__":
    unittest.main()

class FootageSourcePolicyTests(unittest.TestCase):
    def test_open_web_sources_are_prioritized_over_stock_libraries(self):
        from main import FOOTAGE_SOURCE_PRIORITY
        self.assertLess(
            FOOTAGE_SOURCE_PRIORITY.index("Google Search"),
            FOOTAGE_SOURCE_PRIORITY.index("Pexels"),
        )
        self.assertLess(
            FOOTAGE_SOURCE_PRIORITY.index("Internet Archive"),
            FOOTAGE_SOURCE_PRIORITY.index("Pixabay"),
        )
        self.assertLess(
            FOOTAGE_SOURCE_PRIORITY.index("NASA"),
            FOOTAGE_SOURCE_PRIORITY.index("Pixabay"),
        )

    def test_google_result_parser_extracts_direct_video_link(self):
        from main import _GoogleResultParser
        parser = _GoogleResultParser()
        parser.feed('<a href="https://example.org/forest.mp4">Public Domain forest video</a>')
        self.assertEqual(
            parser.results,
            [("https://example.org/forest.mp4", "Public Domain forest video")],
        )

