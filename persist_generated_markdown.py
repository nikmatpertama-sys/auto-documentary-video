#!/usr/bin/env python3
"""Commit only generated Markdown deletions made in this GitHub Actions run."""

import os
import subprocess
import sys
from pathlib import Path

EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
PERMANENT_FILES = {"readme.md", "scriptwriter_master_prompt.md"}


def git(*args, check=True):
    return subprocess.run(
        ["git", *args], check=check, capture_output=True, text=False
    )


def is_generated_markdown(relative_path):
    path = Path(relative_path)
    lower_name = path.name.lower()
    if lower_name in PERMANENT_FILES or path.suffix.lower() != ".md":
        return False
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    return "upgrade" in lower_name or (
        "validation" in lower_name and "report" in lower_name
    )


def main():
    # This helper is deliberately a no-op outside GitHub Actions. Local cleanup
    # is handled by cleanup_old_markdown.sh / main.py and must not push anywhere.
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        print("GitHub Markdown persistence skipped outside GitHub Actions.")
        return 0

    if os.getenv("GITHUB_REF_TYPE", "branch") != "branch":
        print("GitHub Markdown persistence skipped because this run is not on a branch.")
        return 0

    branch = os.getenv("GITHUB_REF_NAME", "").strip()
    if not branch:
        print("Cannot persist Markdown cleanup: GITHUB_REF_NAME is missing.", file=sys.stderr)
        return 2

    tracked = git("ls-files", "-z").stdout.split(b"\0")
    deleted = []
    for raw_path in tracked:
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        if is_generated_markdown(relative) and not Path(relative).exists():
            deleted.append(relative)

    if not deleted:
        print("No tracked generated Markdown deletions to persist.")
        return 0

    # Stage only matching files that are already tracked and now absent.
    git("--literal-pathspecs", "add", "-u", "--", *deleted)
    staged_raw = git("diff", "--cached", "--name-only", "-z").stdout.split(b"\0")
    staged = {
        value.decode("utf-8", errors="surrogateescape")
        for value in staged_raw if value
    }
    if staged - set(deleted):
        print(
            "Refusing to commit because unrelated staged paths were detected: "
            + ", ".join(sorted(staged - set(deleted))),
            file=sys.stderr,
        )
        return 2

    if not staged:
        print("No matching Markdown deletions were staged.")
        return 0

    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git(
        "commit", "-m",
        "chore: remove generated upgrade and validation Markdown [skip ci]",
    )
    git("push", "origin", f"HEAD:refs/heads/{branch}")

    print("Persisted generated Markdown deletions to branch " + branch + ":")
    for path in deleted:
        print("  " + path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
