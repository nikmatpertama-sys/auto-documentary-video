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


def valid_footage_source_state(path):
    """Accept only the tiny public cooldown schema; reject anything resembling secrets."""
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or set(payload) - {"schema_version", "updated_at", "sources"}
    ):
        return False
    try:
        float(payload.get("updated_at", 0))
    except (TypeError, ValueError):
        return False
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return False
    allowed_sources = {
        "Internet Archive", "Wikimedia Commons", "NASA", "Google Search",
        "Pexels", "Pixabay",
    }
    for source, details in sources.items():
        if source not in allowed_sources or not isinstance(details, dict):
            return False
        allowed_fields = {"cooldown_until", "updated_at", "last_status", "reason"}
        if set(details) - allowed_fields:
            return False
        try:
            float(details.get("cooldown_until", 0))
            float(details.get("updated_at", 0))
            int(details.get("last_status", 0) or 0)
        except (TypeError, ValueError):
            return False
        if details.get("reason", "") not in ("", "http_429", "temporary_unavailable"):
            return False
    return True


def main():
    # This helper is deliberately a no-op outside GitHub Actions. Local cleanup
    # and source-state persistence must never push from a user's local machine.
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        print("GitHub persistence skipped outside GitHub Actions.")
        return 0

    if os.getenv("GITHUB_REF_TYPE", "branch") != "branch":
        print("GitHub persistence skipped because this run is not on a branch.")
        return 0

    branch = os.getenv("GITHUB_REF_NAME", "").strip()
    if not branch:
        print("Cannot persist cleanup/state: GITHUB_REF_NAME is missing.", file=sys.stderr)
        return 2

    tracked = git("ls-files", "-z").stdout.split(b"\0")
    deleted = []
    for raw_path in tracked:
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        if is_generated_markdown(relative) and not Path(relative).exists():
            deleted.append(relative)

    allowed_changes = set(deleted)
    if deleted:
        # Stage only matching files that are already tracked and now absent.
        git("--literal-pathspecs", "add", "-u", "--", *deleted)

    # A tiny root-level state file survives failed workflow runs. It contains
    # provider names, cooldown timestamps, status codes and short reasons only;
    # API keys, topic text, candidate URLs and user data are never stored here.
    source_state = Path("footage_source_state.json")
    if source_state.exists():
        if not valid_footage_source_state(source_state):
            print("Ignoring invalid footage_source_state.json; it was not staged.", file=sys.stderr)
        else:
            git("add", "--", str(source_state))
            allowed_changes.add(str(source_state))

    staged_raw = git("diff", "--cached", "--name-only", "-z").stdout.split(b"\0")
    staged = {
        value.decode("utf-8", errors="surrogateescape")
        for value in staged_raw if value
    }
    if staged - allowed_changes:
        print(
            "Refusing to commit because unrelated staged paths were detected: "
            + ", ".join(sorted(staged - allowed_changes)),
            file=sys.stderr,
        )
        return 2

    if not staged:
        print("No generated Markdown deletions or source cooldown state changes to persist.")
        return 0

    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    message_parts = []
    if deleted:
        message_parts.append("remove generated Markdown")
    if str(source_state) in staged:
        message_parts.append("persist footage source cooldowns")
    git("commit", "-m", "chore: " + " and ".join(message_parts) + " [skip ci]")
    git("push", "origin", f"HEAD:refs/heads/{branch}")

    if deleted:
        print("Persisted generated Markdown deletions to branch " + branch + ":")
        for path in deleted:
            print("  " + path)
    if str(source_state) in staged:
        print("Persisted non-secret footage source cooldown state to branch " + branch + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
