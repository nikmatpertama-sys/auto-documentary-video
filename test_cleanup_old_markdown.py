from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent
CLEANUP_SCRIPT = PROJECT_ROOT / "cleanup_old_markdown.sh"


class CleanupOldMarkdownTests(unittest.TestCase):
    def test_only_generated_markdown_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shutil.copy2(CLEANUP_SCRIPT, root / "cleanup_old_markdown.sh")

            files = {
                "UPGRADE_10.9.md": False,
                "docs/upgrade_notes.MD": False,
                "reports/validation_report.md": False,
                "README.md": True,
                "SCRIPTWRITER_MASTER_PROMPT.md": True,
                "docs/project_notes.md": True,
                ".git/UPGRADE_INTERNAL.md": True,
                "venv/UPGRADE_ENV.md": True,
                "node_modules/upgrade_vendor.md": True,
            }
            for relative, should_remain in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")

            completed = subprocess.run(
                ["bash", str(root / "cleanup_old_markdown.sh")],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            for relative, should_remain in files.items():
                self.assertEqual((root / relative).exists(), should_remain, relative)
            self.assertIn("3 file Markdown generated dihapus", completed.stdout)


if __name__ == "__main__":
    unittest.main()
