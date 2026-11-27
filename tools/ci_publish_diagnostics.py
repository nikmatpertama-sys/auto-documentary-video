#!/usr/bin/env python3
"""Publish a run's diagnostics to the ci-diagnostics branch, robustly.

The step this replaces used ``set -euo pipefail`` and could abort on a single
missing file, which hid real pipeline failures: the run's report never reached
the branch and the Actions log needs account access to read. This script never
aborts on a missing artifact, keeps every run under ``runs/<run_id>/`` as well
as ``latest/``, retries the push with a rebase, and turns its own failure into a
public annotation so the reason is visible without log access.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPORTS = (
    "credential_status.json",
    "failure_report.json",
    "stage_report.json",
    "production_report.json",
    "fact_check.json",
    "footage_verification.json",
    "copyright_report.json",
    "qc_report.json",
    "plan.json",
    "timing.json",
    "credits.txt",
    "research.json",
    "voiceover_manifest.json",
    "research_citations.json",
)


def run_slug() -> str:
    """Filesystem-safe topic name for the per-run archive."""
    topic = os.getenv("INPUT_TOPIC", "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", topic).strip("-")
    return slug[:60] or "run"


def annotate(level: str, title: str, message: str) -> None:
    """Emit a GitHub annotation; visible on the public check-run API."""
    safe = re.sub(r"\s+", " ", str(message or "")).strip()[:3000]
    print(f"::{level} title={title}::{safe}", flush=True)


def redacted(text: str) -> str:
    text = re.sub(r"(sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{10,}|gsk_[A-Za-z0-9]{8,})", "[redacted]", text)
    text = re.sub(r"(Bearer\s+)\S+", r"\1[redacted]", text)
    return text


def run(args: list[str], cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {(result.stderr or result.stdout)[-600:]}")
    return result


def report_failure() -> None:
    """Annotate the stage and error the pipeline actually stopped on."""
    report_path = Path("failure_report.json")
    if not report_path.is_file():
        annotate("warning", "No failure report", "The pipeline failed before writing failure_report.json.")
        log = Path("output/pipeline.log")
        if log.is_file():
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-15:])
            annotate("error", "Pipeline log tail", redacted(tail))
        return
    try:
        report = json.loads(report_path.read_text())
    except ValueError:
        annotate("error", "Unreadable failure report", "failure_report.json is not valid JSON.")
        return
    annotate(
        "error",
        "Pipeline stage: " + str(report.get("current_stage") or "unknown"),
        f"{report.get('error_type')}: {redacted(str(report.get('error_message') or ''))[:1800]}",
    )
    stage_report = Path("stage_report.json")
    if stage_report.is_file():
        try:
            stages = json.loads(stage_report.read_text())
        except ValueError:
            stages = {}
        if isinstance(stages, dict):
            summary = ", ".join(
                f"{name}={value.get('status')}"
                for name, value in stages.items()
                if isinstance(value, dict)
            )
            if summary:
                annotate("notice", "Stage statuses", summary[:1800])


def copy_artifacts(target: Path) -> list[str]:
    copied: list[str] = []
    out = target / "output"
    out.mkdir(parents=True, exist_ok=True)
    for name in REPORTS:
        source = Path(name)
        if name == "voiceover_manifest.json":
            source = Path("output") / name
        if source.is_file():
            shutil.copy2(source, target / name)
            copied.append(name)
    for relative in ("output/pipeline.log", "output/captions.ass", "output/speechify_manifest.json"):
        source = Path(relative)
        if not source.is_file():
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".log":
            lines = source.read_text(errors="replace").splitlines()[-3000:]
            destination.write_text(redacted("\n".join(lines)))
        else:
            shutil.copy2(source, destination)
        copied.append(relative)
    vision = Path("output/vision")
    if vision.is_dir():
        shutil.copytree(vision, target / "output/vision", dirs_exist_ok=True)
        copied.append("output/vision")
    return copied


def capture_video(target: Path) -> list[str]:
    """Record the finished file's real metadata and a few review frames."""
    video = Path("documentary.mp4")
    if not video.is_file():
        return []
    captured = []
    probe = run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video),
    ])
    if probe.returncode == 0 and probe.stdout.strip():
        (target / "video_ffprobe.json").write_text(probe.stdout)
        captured.append("video_ffprobe.json")
    frames = run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-vf", "fps=1/5,scale=360:-2", "-frames:v", "24", "-q:v", "6",
        str(target / "frame_%02d.jpg"),
    ])
    if frames.returncode == 0:
        captured.append("frames")
    return captured


def write_run_file(target: Path) -> None:
    def env(name: str) -> str:
        return os.getenv(name, "").strip()

    target.joinpath("RUN.txt").write_text(
        "\n".join([
            f"run={env('GITHUB_RUN_ID')}",
            f"sha={env('GITHUB_SHA')}",
            f"branch={env('GITHUB_REF_NAME')}",
            f"job_status={env('JOB_STATUS')}",
            f"topic={env('INPUT_TOPIC')}",
            f"pipeline_exit={env('PIPELINE_EXIT')}",
        ]) + "\n"
    )


def publish(clone: Path) -> None:
    for attempt in range(1, 6):
        run(["git", "-C", str(clone), "add", "-A"])
        run([
            "git", "-C", str(clone), "-c", "user.name=documentary-ci",
            "-c", "user.email=documentary-ci@users.noreply.github.com",
            "commit", "-m", f"diagnostics for run {os.getenv('GITHUB_RUN_ID', 'local')}",
        ])
        push = run(["git", "-C", str(clone), "push", "origin", "HEAD:ci-diagnostics"])
        if push.returncode == 0:
            print(f"Published diagnostics on attempt {attempt}.")
            return
        print(f"Push attempt {attempt} failed: {(push.stderr or push.stdout)[-400:]}")
        run(["git", "-C", str(clone), "pull", "--rebase", "--autostash", "origin", "ci-diagnostics"])
        time.sleep(4)
    annotate("warning", "Diagnostics push failed", "The reports remain in the run artifacts.")


def main() -> int:
    report_failure()

    token = os.getenv("GH_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        annotate("warning", "Diagnostics not published", "GH_TOKEN or GITHUB_REPOSITORY is empty.")
        return 0

    clone = Path(tempfile.mkdtemp(prefix="diag_")) / "repo"
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    fresh = run(["git", "clone", "--depth", "1", "--branch", "ci-diagnostics", url, str(clone)])
    if fresh.returncode != 0:
        clone.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", str(clone)], check=True)
        run(["git", "-C", str(clone), "remote", "add", "origin", url], check=True)
        run(["git", "-C", str(clone), "checkout", "-q", "--orphan", "ci-diagnostics"])
        for entry in clone.iterdir():
            if entry.name != ".git":
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()

    latest = clone / "latest"
    shutil.rmtree(latest, ignore_errors=True)
    written = copy_artifacts(latest)
    written += capture_video(latest)
    write_run_file(latest)

    run_id = os.getenv("GITHUB_RUN_ID", "local")
    # A suite runs several topics under one run id, so the archive is keyed by
    # topic as well; otherwise each job overwrote the previous job's reports.
    archived = clone / "runs" / run_id / (run_slug() if os.getenv("INPUT_TOPIC") else "run")
    archived.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(archived, ignore_errors=True)
    shutil.copytree(latest, archived)

    annotate("notice", "Diagnostics written", ", ".join(written) or "nothing to publish")
    publish(clone)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - annotate instead of dying silently
        annotate("error", "Diagnostics step failed", redacted(f"{type(error).__name__}: {error}"))
        raise SystemExit(1)
