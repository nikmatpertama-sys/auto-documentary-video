import unittest

from scene_normalization import normalize_scene_count


def scene(narration, focus, query):
    return {
        "narration": narration,
        "visual_focus": focus,
        "visual_query": query,
        "visual_queries": [query + " close up", query + " wide shot"],
        "visual_mode": "footage",
        "reference_query": query,
        "source_url": "",
    }


class SceneNormalizationTests(unittest.TestCase):
    def test_splits_two_sentence_scene_and_remaps_claims(self):
        scenes = [
            scene("The river basin carries water. The river basin shapes the valley.", "river basin", "river basin aerial"),
            scene("A second fact is visible.", "river flow", "river flow rocks"),
            scene("A third fact is visible.", "forest canopy", "forest canopy"),
            scene("A fourth fact is visible.", "rain forest", "rain forest"),
            scene("A fifth fact is visible.", "tropical leaves", "tropical leaves"),
            scene("A sixth fact is visible.", "water droplets", "water droplets"),
            scene("A seventh fact is visible.", "cloud formation", "cloud formation"),
        ]
        claims = [
            {"scene": 1, "sentence": "The river basin carries water.", "source_urls": ["https://a.example/1"]},
            {"scene": 1, "sentence": "The river basin shapes the valley.", "source_urls": ["https://a.example/2"]},
        ]
        result = normalize_scene_count({"scenes": scenes, "fact_claims": claims})
        self.assertEqual(len(result["scenes"]), 8)
        self.assertEqual(result["scenes"][0]["narration"], "The river basin carries water.")
        self.assertEqual(result["scenes"][1]["narration"], "The river basin shapes the valley.")
        self.assertEqual([x["scene"] for x in result["fact_claims"]], [1, 2])

    def test_merges_adjacent_scenes_only_when_subject_and_query_match(self):
        scenes = [
            scene("The Amazon canopy captures rainfall.", "Amazon canopy", "Amazon canopy rainforest"),
            scene("The Amazon canopy releases moisture.", "Amazon canopy", "Amazon canopy rainforest trees"),
        ]
        for i in range(3, 10):
            scenes.append(scene(f"Scene {i} is supported by evidence.", f"subject {i}", f"subject {i} footage"))
        claims = [
            {"scene": 1, "sentence": "The Amazon canopy captures rainfall.", "source_urls": ["https://a.example/1"]},
            {"scene": 2, "sentence": "The Amazon canopy releases moisture.", "source_urls": ["https://a.example/2"]},
        ]
        result = normalize_scene_count({"scenes": scenes, "fact_claims": claims})
        self.assertEqual(len(result["scenes"]), 8)
        self.assertEqual(
            result["scenes"][0]["narration"],
            "The Amazon canopy captures rainfall. The Amazon canopy releases moisture.",
        )
        self.assertEqual([x["scene"] for x in result["fact_claims"]], [1, 1])

    def test_refuses_to_merge_unrelated_scenes(self):
        subjects = [
            ("A fox crosses the field.", "red fox", "red fox field"),
            ("Volcanic ash drifts upward.", "volcanic ash", "volcanic ash plume"),
            ("A ship enters the harbor.", "cargo ship", "cargo ship harbor"),
            ("A glacier breaks apart.", "glacier ice", "glacier ice breaking"),
            ("A satellite passes overhead.", "weather satellite", "weather satellite orbit"),
            ("A coral colony grows slowly.", "coral colony", "coral reef colony"),
            ("A desert beetle moves across sand.", "desert beetle", "desert beetle sand"),
            ("A turbine spins in the wind.", "wind turbine", "wind turbine blades"),
            ("A lava stream flows downhill.", "lava stream", "lava stream volcano"),
        ]
        scenes = [scene(*item) for item in subjects]
        candidate = {"scenes": scenes, "fact_claims": []}
        result = normalize_scene_count(candidate)
        self.assertIs(result, candidate)
        self.assertEqual(len(result["scenes"]), 9)

    def test_does_not_invent_an_eighth_narration_sentence(self):
        scenes = [
            scene(f"Scene {i} has one sentence.", f"unique focus {i}", f"unique focus {i} footage")
            for i in range(1, 8)
        ]
        candidate = {"scenes": scenes, "fact_claims": []}
        self.assertIs(normalize_scene_count(candidate), candidate)


if __name__ == "__main__":
    unittest.main()
