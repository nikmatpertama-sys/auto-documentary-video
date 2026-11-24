import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from hook_overlay import create_hook_overlay, _load_font
from caption_timing import align_boundary_text, build_phrase_groups, karaoke_phrase, group_bounds
from reference_visuals import (
    needs_reference_visual,
    allowed_reuse_license,
    has_trusted_institution_url,
    validate_reference_candidate,
)
from source_visuals import compose_reference_frame, nasa_asset_reuse_eligible


class HookOverlayTests(unittest.TestCase):
    def test_long_unicode_hook_is_wrapped_and_preserved(self):
        hook = "The Amazon’s trees create their own rain — and scientists can trace it."
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "hook.png"
            layout = create_hook_overlay(
                "The Amazon Rainforest: The Forest That Makes Its Own Rain",
                hook,
                output,
            )
            self.assertTrue(output.exists())
            with Image.open(output) as image:
                self.assertEqual(image.size, (1080, 1920))
            self.assertGreaterEqual(len(layout["hook_lines"]), 2)
            self.assertEqual(" ".join(layout["hook_lines"]), hook)
            self.assertEqual(" ".join(layout["title_lines"]), "THE AMAZON RAINFOREST: THE FOREST THAT MAKES ITS OWN RAIN")
            self.assertEqual(layout["hook_text"], hook)
            self.assertTrue(any("AMAZON RAINFOREST" in x for x in layout["title_lines"]))
            draw = ImageDraw.Draw(Image.new("RGB", (1080, 1920)))
            for line in layout["title_lines"]:
                bbox = draw.textbbox((0, 0), line, font=_load_font(layout["title_font_size"]))
                self.assertLessEqual(bbox[2] - bbox[0], layout["max_text_width"])
            for line in layout["hook_lines"]:
                bbox = draw.textbbox((0, 0), line, font=_load_font(layout["hook_font_size"]))
                self.assertLessEqual(bbox[2] - bbox[0], layout["max_text_width"])
            self.assertLess(layout["card_bounds"][3], 1700)

    def test_extremely_long_hook_fails_instead_of_being_clipped(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                create_hook_overlay("Very long title", "word " * 1000, Path(temp) / "too_long.png")


class CaptionTimingTests(unittest.TestCase):
    def test_original_punctuation_is_restored(self):
        sentence = "Deep within the Amazon, trees summon their own rain."
        words = ["Deep", "within", "the", "Amazon", "trees", "summon", "their", "own", "rain"]
        boundaries = [
            {"text": word, "offset": i * 0.25, "duration": 0.19}
            for i, word in enumerate(words)
        ]
        aligned = align_boundary_text(sentence, boundaries)
        self.assertEqual(len(aligned), 9)
        self.assertEqual(aligned[3]["text"], "Amazon,")
        self.assertEqual(aligned[-1]["text"], "rain.")

    def test_unmatched_tts_words_fail_instead_of_silently_dropping_caption_words(self):
        with self.assertRaises(ValueError):
            align_boundary_text(
                "This caption has missing words.",
                [{"text": "This", "offset": 0.0, "duration": 0.2},
                 {"text": "caption", "offset": 0.3, "duration": 0.2}],
            )

    def test_phrase_groups_follow_clause_and_sentence_boundaries(self):
        sentence1 = "Deep within the Amazon, trees summon their own rain."
        words1 = ["Deep", "within", "the", "Amazon", "trees", "summon", "their", "own", "rain"]
        boundaries1 = [
            {"text": word, "offset": i * 0.28, "duration": 0.20}
            for i, word in enumerate(words1)
        ]
        first = align_boundary_text(sentence1, boundaries1)
        # Attach a second sentence with a deliberate pause.
        second = [
            {"text": "Leaves", "offset": 4.0, "duration": 0.25},
            {"text": "release", "offset": 4.28, "duration": 0.27},
            {"text": "water", "offset": 4.58, "duration": 0.25},
            {"text": "vapor.", "offset": 4.85, "duration": 0.30},
        ]
        second = [{**item, "sentence_index": 1} for item in second]
        first = [{**item, "sentence_index": 0} for item in first]
        groups = build_phrase_groups(first + second)
        self.assertGreaterEqual(len(groups), 3)
        combined = [" ".join(w["text"] for w in g) for g in groups]
        self.assertTrue(any(g.endswith("Amazon,") for g in combined))
        self.assertTrue(any(g.endswith("rain.") for g in combined))
        self.assertTrue(any(g.endswith("vapor.") for g in combined))
        for group in groups:
            start, end = group_bounds(group)
            self.assertLess(start, end)
            self.assertIn("\\kf", karaoke_phrase(group))


class ReferenceVisualPolicyTests(unittest.TestCase):
    def test_map_and_diagram_scenes_request_published_reference(self):
        self.assertTrue(needs_reference_visual({"visual_query": "satellite image of Amazon river map"}))
        self.assertTrue(needs_reference_visual({"visual_mode": "trusted_reference", "visual_query": "macaw"}))
        self.assertTrue(needs_reference_visual({"visual_query": "published scientific animation of the water cycle"}))
        self.assertTrue(needs_reference_visual({"visual_query": "scientific illustration of leaf anatomy"}))
        self.assertFalse(needs_reference_visual({"visual_query": "macaw flying over rainforest"}))
        self.assertFalse(needs_reference_visual({"visual_query": "rainforest landscape", "narration": "This model can explain the process."}))
        self.assertFalse(needs_reference_visual({"visual_query": "scientific overview", "visual_mode": "footage"}, scene_index=1))
        self.assertFalse(needs_reference_visual({"visual_query": "satellite map", "visual_mode": "trusted_reference"}, scene_index=1))

    def test_reusable_license_policy_rejects_nc_and_nd(self):
        self.assertTrue(allowed_reuse_license("CC BY-SA 4.0"))
        self.assertTrue(allowed_reuse_license("Public domain"))
        self.assertFalse(allowed_reuse_license("CC BY-NC 4.0"))
        self.assertFalse(allowed_reuse_license("CC BY-ND 4.0"))
        self.assertFalse(allowed_reuse_license("All rights reserved"))

    def test_reference_requires_institutional_origin_and_matching_metadata(self):
        candidate = {
            "source": "Wikimedia Commons",
            "url": "https://upload.wikimedia.org/example.jpg",
            "page_url": "https://commons.wikimedia.org/wiki/File:Amazon_river_satellite.jpg",
            "original_source_url": "https://earthobservatory.nasa.gov/example",
            "title": "Amazon River satellite image",
            "metadata": "Amazon basin satellite image from NASA Earth Observatory",
            "publisher": "NASA Earth Observatory",
            "creator": "NASA",
            "license": "CC BY 4.0",
        }
        ok, reason = validate_reference_candidate(candidate, "Amazon river satellite image")
        self.assertTrue(ok, reason)
        self.assertTrue(has_trusted_institution_url(candidate["original_source_url"]))

    def test_commons_candidate_without_linked_institutional_origin_is_rejected(self):
        candidate = {
            "source": "Wikimedia Commons", "url": "https://upload.wikimedia.org/example.jpg",
            "page_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "title": "Amazon satellite image", "metadata": "Amazon satellite image",
            "publisher": "NASA", "creator": "NASA", "license": "CC BY 4.0",
        }
        ok, reason = validate_reference_candidate(candidate, "Amazon satellite image")
        self.assertFalse(ok)
        self.assertIn("linked primary source", reason)

    def test_nasa_third_party_or_restricted_material_is_skipped(self):
        self.assertTrue(nasa_asset_reuse_eligible("NASA Earth Observatory image of Amazon basin"))
        self.assertFalse(nasa_asset_reuse_eligible("Copyright protected material used with permission"))

    def test_reference_frame_contains_the_original_image_without_crop(self):
        with tempfile.TemporaryDirectory() as temp:
            original = Path(temp) / "original.jpg"
            framed = Path(temp) / "framed.jpg"
            Image.new("RGB", (1600, 900), (220, 220, 220)).save(original)
            compose_reference_frame(
                original, framed, "Amazon River Satellite Image",
                "NASA Earth Observatory", "https://earthobservatory.nasa.gov/example",
            )
            self.assertTrue(framed.exists())
            with Image.open(framed) as image:
                self.assertEqual(image.size, (1080, 1920))


if __name__ == "__main__":
    unittest.main()
