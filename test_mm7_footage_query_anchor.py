"""Fallback footage queries must keep the scene's actual subject.

A live run for "Ant colony life cycle" searched generic insect phrases
("insect macro nature", "wild insect crawling on plant"), downloaded 25 beetles,
bees and crickets, and the visual gate correctly rejected every one. The
fallback is meant to broaden the shot, not drop the animal.
"""

import unittest

import main


class FootageQueryAnchorTests(unittest.TestCase):
    def test_scene_subject_phrase_keeps_the_subject_and_drops_shot_words(self):
        self.assertEqual(main.scene_subject_phrase("queen ant close up macro"), "queen ant")
        self.assertIn("ant", main.scene_subject_phrase("ant colony entrance wide shot"))

    def test_sparse_pool_fallbacks_keep_the_subject(self):
        queries = main.fallback_visual_queries(["queen ant close up"], "The queen ant lays eggs.")
        self.assertTrue(queries)
        insect_queries = [query for query in queries if "macro" in query or "leaf" in query]
        self.assertTrue(insect_queries)
        for query in insect_queries:
            self.assertIn("ant", query.lower())

    def test_adaptive_retry_keeps_the_subject_in_every_insect_query(self):
        scene = {
            "visual_query": "queen ant close up",
            "narration": "The queen ant lays thousands of eggs inside the colony.",
        }
        queries = main.adaptive_visual_retry_queries(scene, [{"reason": "shows a beetle, not an ant"}])
        self.assertTrue(queries)
        for query in queries:
            self.assertIn("ant", query.lower())

    def test_subject_is_not_required_when_the_scene_has_none(self):
        self.assertEqual(main.scene_subject_phrase(""), "")


if __name__ == "__main__":
    unittest.main()
