import unittest

from visual_search_expansion import (
    build_visual_search_fallbacks,
    effective_candidate_limit,
    prepare_footage_queries,
    expand_visual_search_variants,
)


class VisualSearchExpansionTests(unittest.TestCase):
    def test_mist_query_broadens_to_rainforest_canopy_without_mist(self):
        queries = build_visual_search_fallbacks(
            "mist rising from tropical rainforest treetops"
        )
        self.assertIn("tropical rainforest canopy", queries)
        self.assertIn("tropical rainforest treetops", queries)
        self.assertTrue(all("mist" not in q.lower() for q in queries))

    def test_specific_animal_is_preserved(self):
        queries = build_visual_search_fallbacks(
            "close up jaguar moving through dense rainforest"
        )
        self.assertIn("jaguar in tropical rainforest", queries)
        self.assertTrue(all("jaguar" in q.lower() for q in queries))

    def test_orchid_subject_is_preserved(self):
        queries = build_visual_search_fallbacks(
            "orchid flower on a tropical rainforest branch"
        )
        self.assertIn("orchid in tropical rainforest", queries)
        self.assertTrue(all("orchid" in q.lower() for q in queries))

    def test_fallbacks_are_unique_and_exclude_original_queries(self):
        original = "tropical rainforest canopy"
        queries = build_visual_search_fallbacks(
            original, ["jungle canopy"]
        )
        lower = [q.lower() for q in queries]
        self.assertEqual(len(lower), len(set(lower)))
        self.assertNotIn(original, lower)
        self.assertNotIn("jungle canopy", lower)

    def test_non_nature_core_query_is_cleaned(self):
        queries = build_visual_search_fallbacks(
            "cinematic close up of electricity flowing through copper wires"
        )
        self.assertIn("electricity flowing copper wires", queries)

    def test_alternative_shots_are_expanded_independently(self):
        queries = build_visual_search_fallbacks(
            "tropical rainforest canopy",
            ["insects on wet rainforest leaves", "orchid on rainforest branch"],
        )
        self.assertIn("tropical rainforest insects", queries)
        self.assertIn("orchid in tropical rainforest", queries)


    def test_reference_artifact_word_is_removed_from_real_footage_search(self):
        queries = prepare_footage_queries(
            "tropical rainforest rainfall map",
            ["global precipitation map Amazon region", "rainfall chart rainforest"],
        )
        self.assertEqual(queries[0], "tropical rainforest rainfall")
        self.assertIn("global precipitation amazon region", [q.lower() for q in queries])
        self.assertTrue(all(not any(word in q.lower().split() for word in ("map", "chart", "graph", "diagram")) for q in queries))


    def test_search_variants_interleave_independent_shots(self):
        queries = expand_visual_search_variants([
            "rain falling in tropical rainforest",
            "macaw flying over rainforest canopy",
            "orchid close up on rainforest branch",
        ])
        self.assertGreaterEqual(len(queries), 3)
        first_three = " | ".join(queries[:3]).lower()
        self.assertIn("rain", first_three)
        self.assertIn("macaw", first_three)
        self.assertIn("orchid", first_three)

    def test_rainfall_reference_request_becomes_real_footage_variants(self):
        queries = expand_visual_search_variants([
            "tropical rainforest rainfall map",
            "global precipitation map Amazon region",
            "rainfall chart rainforest",
        ])
        self.assertIn("rain falling in tropical rainforest", queries)
        self.assertIn("raindrops on tropical rainforest leaves", queries)
        self.assertTrue(all(not any(word in q.lower().split() for word in ("map", "chart", "graph", "diagram")) for q in queries))

    def test_rainfall_map_fallback_generates_real_rainforest_shots(self):
        queries = build_visual_search_fallbacks(
            "tropical rainforest rainfall map",
            ["global precipitation map Amazon region"],
        )
        self.assertIn("rain falling in tropical rainforest", queries)
        self.assertIn("raindrops on tropical rainforest leaves", queries)
        self.assertTrue(all("map" not in q.lower().split() for q in queries))

    def test_legacy_workflow_limit_five_is_raised_to_eight(self):
        self.assertEqual(effective_candidate_limit("5"), 8)

    def test_candidate_limit_is_clamped_and_handles_invalid_values(self):
        self.assertEqual(effective_candidate_limit("11"), 11)
        self.assertEqual(effective_candidate_limit("99"), 12)
        self.assertEqual(effective_candidate_limit("invalid"), 8)


if __name__ == "__main__":
    unittest.main()
