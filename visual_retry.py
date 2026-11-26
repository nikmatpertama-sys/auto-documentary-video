"""Bounded retry policy for temporary visual-verification provider failures."""
from __future__ import annotations

import re
from typing import Tuple


def _retry_hint_seconds(detail: str, default: float) -> float:
    text = str(detail or "")
    patterns = (
        r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*s",
        r"retry[- ]after[\s:=]+([0-9]+(?:\.[0-9]+)?)\s*(?:s|sec|seconds)?",
        r"retryDelay[\"']?\s*[:=]\s*[\"']([0-9]+(?:\.[0-9]+)?)(ms|s|m)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = float(match.group(1))
            unit = (match.group(2) or "s").lower() if match.lastindex and match.lastindex >= 2 else "s"
            if unit == "ms":
                value /= 1000.0
            elif unit == "m":
                value *= 60.0
            return max(1.0, min(900.0, value))
        except (TypeError, ValueError):
            continue
    return float(default)


def visual_retry_policy(
    provider: str,
    status_code: int,
    detail: str = "",
    attempt_number: int = 1,
    max_attempts: int = 3,
) -> Tuple[bool, float, str]:
    """Return (should_retry, wait_seconds, reason) for a visual provider.

    OpenRouter's free-model daily cap is account-wide and cannot be fixed by
    retrying another free model/key in the same account during this run.
    Authentication/permission errors are configuration failures, not retries.
    """
    provider_name = str(provider or "").strip().lower()
    message = str(detail or "").lower()
    status = int(status_code or 0)
    attempt = max(1, int(attempt_number or 1))
    limit = max(1, int(max_attempts or 1))

    if attempt >= limit:
        return False, 0.0, f"provider retry budget exhausted ({attempt}/{limit} attempts)"

    if status == 429:
        if provider_name.startswith("openrouter") and any(
            marker in message
            for marker in (
                "free-models-per-day",
                "openrouter_free_tier_daily",
                "free model requests per day",
            )
        ):
            return False, 0.0, "OpenRouter account-wide free-model daily quota is exhausted"
        delay = _retry_hint_seconds(detail, 45.0)
        # A small floor avoids repeatedly hitting token-per-minute limits before
        # provider counters have had time to roll over.
        delay = max(30.0, delay)
        return True, min(900.0, delay), "temporary rate limit; retry after provider reset"

    if status in (408, 425) or 500 <= status <= 599:
        default = 25.0 if status in (408, 425) else 20.0
        delay = _retry_hint_seconds(detail, default)
        return True, max(10.0, min(180.0, delay)), f"temporary provider HTTP {status}"

    return False, 0.0, f"non-retryable provider HTTP {status}"


def redact_provider_error(detail: str, secrets=(), max_chars: int = 700) -> str:
    """Return a log-safe provider error excerpt without known credentials."""
    value = str(detail or "").replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        token = str(secret or "")
        if len(token) >= 8:
            value = value.replace(token, "[REDACTED]")
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    return value[:max(0, int(max_chars))]
