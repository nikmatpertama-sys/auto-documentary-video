"""Production diagnostics: stage timings, failure reports, and secret redaction.

The documentary pipeline is long-running and spends real API quota, so a failed
run must leave behind enough structured evidence to explain *where* it stopped
and *why*, without ever writing credentials to disk or to the job log.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Environment variables whose *names* indicate a credential. Their values are
# never written to reports; only their presence is recorded.
_SECRET_NAME_RE = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}\b"),
)

MAX_TRACEBACK_CHARS = 6000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def secret_values() -> list[str]:
    """Collect configured credential values so they can be scrubbed from output."""
    values = []
    for name, value in os.environ.items():
        if not _SECRET_NAME_RE.search(name):
            continue
        value = str(value or "").strip()
        if len(value) >= 6:
            values.append(value)
    return sorted(values, key=len, reverse=True)


def redact(text, extra_secrets=()) -> str:
    """Remove credential-looking content from a diagnostic string."""
    result = str(text if text is not None else "")
    for value in list(extra_secrets) + secret_values():
        if value:
            result = result.replace(value, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda match: match.group(1) + "[REDACTED]", result)
    return result


class StageLog:
    """Ordered, JSON-serialisable record of every pipeline stage."""

    def __init__(self) -> None:
        self._stages: list[dict] = []
        self._started = time.monotonic()

    def begin(self, name: str, **details) -> dict:
        """Open a stage without requiring a re-indented ``with`` block."""
        record = {
            "stage": name,
            "status": "running",
            "started_at": _now(),
            "details": {key: redact(value) for key, value in details.items()},
            "_started_monotonic": time.monotonic(),
        }
        self._stages.append(record)
        print(f"[stage] {name}: start", flush=True)
        return record

    def _running(self):
        for record in reversed(self._stages):
            if record.get("status") == "running":
                return record
        return None

    def complete(self, **details) -> None:
        record = self._running()
        if record is None:
            return
        elapsed = time.monotonic() - record.pop("_started_monotonic", time.monotonic())
        record.update(status="completed", duration_seconds=round(elapsed, 3))
        if details:
            record.setdefault("details", {}).update(
                {key: redact(value) for key, value in details.items()}
            )
        print(f"[stage] {record['stage']}: completed in {elapsed:.1f}s", flush=True)

    def fail(self, exc: BaseException | None = None) -> None:
        record = self._running()
        if record is None:
            return
        elapsed = time.monotonic() - record.pop("_started_monotonic", time.monotonic())
        record.update(status="failed", duration_seconds=round(elapsed, 3))
        if exc is not None:
            record["error"] = redact(f"{type(exc).__name__}: {exc}")[:2000]
        print(f"[stage] {record['stage']}: FAILED after {elapsed:.1f}s", flush=True)

    @contextmanager
    def stage(self, name: str, **details):
        record = self.begin(name, **details)
        try:
            yield record
        except BaseException as exc:
            self.fail(exc)
            raise
        else:
            self.complete()

    @property
    def stages(self) -> list[dict]:
        return [
            {key: value for key, value in item.items() if key != "_started_monotonic"}
            for item in self._stages
        ]

    def current_stage(self) -> str:
        for record in reversed(self._stages):
            if record.get("status") == "running":
                return str(record.get("stage"))
        return self._stages[-1]["stage"] if self._stages else "not_started"

    def summary(self) -> dict:
        """Compact timing summary for the production report."""
        failed = [item for item in self._stages if item.get("status") == "failed"]
        failed = failed or [item for item in self._stages if item.get("status") == "running"]
        return {
            "total_elapsed_seconds": round(time.monotonic() - self._started, 3),
            "stage_count": len(self._stages),
            "failed_stage": failed[-1]["stage"] if failed else None,
            "stages": self.stages,
        }


STAGES = StageLog()
stage = STAGES.stage


def _command_version(command: str, args: list[str]) -> dict:
    try:
        result = subprocess.run(
            [command, *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        first_line = (result.stdout or result.stderr or "").strip().splitlines()
        return {
            "available": result.returncode == 0,
            "version": redact(first_line[0])[:200] if first_line else "",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "version": f"{type(exc).__name__}: {exc}"[:200]}


def environment_summary(tools=("ffmpeg", "ffprobe")) -> dict:
    """Describe the runtime without exposing any credential values."""
    configured = sorted(
        name for name in os.environ if _SECRET_NAME_RE.search(name) and os.environ.get(name)
    )
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "cwd": str(Path.cwd()),
        "tools": {name: _command_version(name, ["-hide_banner", "-version"]) for name in tools},
        "credential_names_present": configured,
    }


def _write_json(path, payload) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return str(target)


def write_stage_report(path, extra=None) -> str:
    payload = {
        "generated_at": _now(),
        "environment": environment_summary(),
        "run": STAGES.summary(),
    }
    if extra:
        payload.update({key: redact(value) for key, value in extra.items()})
    return _write_json(path, payload)


def write_failure_report(path, exc: BaseException, context=None) -> str:
    STAGES.fail(exc)
    payload = {
        "generated_at": _now(),
        "status": "failed",
        "failed_stage": STAGES.current_stage(),
        "error_type": type(exc).__name__,
        "error_message": redact(str(exc))[:4000],
        "context": {key: redact(value) for key, value in (context or {}).items()},
        "traceback": redact(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )[-MAX_TRACEBACK_CHARS:],
        "environment": environment_summary(),
        "run": STAGES.summary(),
    }
    return _write_json(path, payload)
