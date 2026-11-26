import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent
PERSIST_SCRIPT = PROJECT_ROOT / "persist_generated_markdown.py"


def run(command, cwd=None, check=True, env=None):
    return subprocess.run(
        command, cwd=cwd, check=check, capture_output=True, text=True, env=env
    )


class PersistGeneratedMarkdownTests(unittest.TestCase):
    def test_workflow_commits_only_generated_markdown_deletions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            remote = base / "remote.git"
            work = base / "work"
            run(["git", "init", "--bare", str(remote)])
            run(["git", "init", "-b", "cleanup-test", str(work)])
            run(["git", "config", "user.name", "test"], cwd=work)
            run(["git", "config", "user.email", "test@example.com"], cwd=work)

            files = {
                "UPGRADE_10.9.md": "remove",
                "docs/validation_report.md": "remove",
                "README.md": "keep",
                "SCRIPTWRITER_MASTER_PROMPT.md": "keep",
                "docs/project_notes.md": "keep",
            }
            for relative, content in files.items():
                path = work / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            run(["git", "add", "-A"], cwd=work)
            run(["git", "commit", "-m", "seed"], cwd=work)
            run(["git", "remote", "add", "origin", str(remote)], cwd=work)
            run(["git", "push", "-u", "origin", "cleanup-test"], cwd=work)

            (work / "UPGRADE_10.9.md").unlink()
            (work / "docs/validation_report.md").unlink()
            env = os.environ.copy()
            env.update({
                "GITHUB_ACTIONS": "true",
                "GITHUB_REF_TYPE": "branch",
                "GITHUB_REF_NAME": "cleanup-test",
            })
            run([sys.executable, str(PERSIST_SCRIPT)], cwd=work, env=env)

            for relative in ("UPGRADE_10.9.md", "docs/validation_report.md"):
                result = run(
                    ["git", "--git-dir", str(remote), "cat-file", "-e", f"cleanup-test:{relative}"],
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, relative)

            for relative in ("README.md", "SCRIPTWRITER_MASTER_PROMPT.md", "docs/project_notes.md"):
                run(["git", "--git-dir", str(remote), "cat-file", "-e", f"cleanup-test:{relative}"])

    def test_does_not_push_when_run_outside_github_actions(self):
        env = os.environ.copy()
        env.pop("GITHUB_ACTIONS", None)
        result = run([sys.executable, str(PERSIST_SCRIPT)], env=env)
        self.assertIn("skipped outside GitHub Actions", result.stdout)


if __name__ == "__main__":
    unittest.main()
