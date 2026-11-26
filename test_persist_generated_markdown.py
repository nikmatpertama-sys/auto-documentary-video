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
                "footage_source_state.json": '{"schema_version":1,"updated_at":1,"sources":{}}',
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
            (work / "footage_source_state.json").write_text(
                '{"schema_version":1,"updated_at":2,"sources":{"Wikimedia Commons":{"cooldown_until":9999999999,"updated_at":2,"last_status":429,"reason":"http_429"}}}',
                encoding="utf-8",
            )
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

            persisted_state = run(
                ["git", "--git-dir", str(remote), "show", "cleanup-test:footage_source_state.json"]
            ).stdout
            self.assertIn("Wikimedia Commons", persisted_state)
            self.assertIn('"last_status":429', persisted_state)


    def test_source_state_validator_rejects_unknown_fields_and_secret_like_payloads(self):
        import json
        import sys
        sys.path.insert(0, str(PROJECT_ROOT))
        from persist_generated_markdown import valid_footage_source_state

        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "footage_source_state.json"
            state.write_text(json.dumps({
                "schema_version": 1,
                "updated_at": 100,
                "sources": {},
                "api_key": "must-not-be-committed",
            }), encoding="utf-8")
            self.assertFalse(valid_footage_source_state(state))

            state.write_text(json.dumps({
                "schema_version": 1,
                "updated_at": 100,
                "sources": {
                    "NASA": {
                        "cooldown_until": 200,
                        "updated_at": 100,
                        "last_status": 429,
                        "reason": "http_429",
                        "access_token": "must-not-be-committed",
                    }
                },
            }), encoding="utf-8")
            self.assertFalse(valid_footage_source_state(state))

    def test_does_not_push_when_run_outside_github_actions(self):
        env = os.environ.copy()
        env.pop("GITHUB_ACTIONS", None)
        result = run([sys.executable, str(PERSIST_SCRIPT)], env=env)
        self.assertIn("skipped outside GitHub Actions", result.stdout)


if __name__ == "__main__":
    unittest.main()
