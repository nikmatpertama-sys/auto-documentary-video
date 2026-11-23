import shutil
import tempfile
import unittest
from pathlib import Path

from visual_fallback import _category, _rainforest_mode, create_fallback_video


class VisualFallbackTests(unittest.TestCase):
    def test_semantic_category_selection(self):
        self.assertEqual(_category({"visual_query": "hydrothermal vent chimney"}, "deep ocean"), "hydrothermal")
        self.assertEqual(_category({"narration": "Bacteria convert chemicals into energy"}, "microbial life"), "bacteria")
        self.assertEqual(_category({"visual_query": "Earth mantle cross section"}, "planet"), "earth")
        self.assertEqual(_category({"visual_query": "unclassified topic"}, "unclassified topic"), "process")

    def test_scene_local_semantics_override_broad_topic_and_prevent_planetary_false_match(self):
        scene = {
            "visual_query": "orchids on rainforest floor",
            "narration": "The abundant life influences planetary climate patterns.",
        }
        self.assertEqual(_category(scene, "space documentary about planets"), "rainforest")
        self.assertEqual(_rainforest_mode(scene), "undergrowth")
        self.assertEqual(_category({"narration": "A planetary system affects climate."}, ""), "climate")

    def test_rainforest_scenes_get_distinct_visual_modes(self):
        self.assertEqual(_rainforest_mode({"visual_query": "jaguar walking in rainforest"}), "jaguar")
        self.assertEqual(_rainforest_mode({"visual_query": "river winding through Amazon forest"}), "river")
        self.assertEqual(_rainforest_mode({"visual_query": "dry riverbed in rainforest"}), "drought")
        self.assertEqual(_rainforest_mode({"visual_query": "single leaf trembling in rainforest"}), "leaf")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_fallback_renders_vertical_h264_video_without_narration_burnin(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "scene.mp4"
            create_fallback_video(
                {"title": "Hydrothermal Vents", "visual_query": "hydrothermal vent chimney",
                 "narration": "Hot mineral-rich water rises through the ocean crust."},
                2, out, 1.2, "Hydrothermal vents",
            )
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 1000)
            probe = shutil.which("ffprobe")
            import subprocess, json
            data = json.loads(subprocess.check_output([
                probe, "-v", "error", "-show_streams", "-of", "json", str(out)
            ], text=True))
            video = next(x for x in data["streams"] if x["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            self.assertEqual(video["codec_name"], "h264")
            self.assertFalse(any(x["codec_type"] == "audio" for x in data["streams"]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_rainforest_fallback_renders_scene_specific_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rainforest.mp4"
            create_fallback_video(
                {"title": "Orchids on rainforest floor", "visual_query": "orchids on rainforest floor",
                 "narration": "Ferns and insects thrive in the wet understory."},
                4, out, 1.0, "Amazon rainforest with planetary climate effects",
            )
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 1000)
            self.assertTrue(out.with_suffix(".png").exists())


if __name__ == "__main__":
    unittest.main()
