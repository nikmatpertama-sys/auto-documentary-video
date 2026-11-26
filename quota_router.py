"""Quota-aware routing for documentary generation providers.

This module deliberately distinguishes measured quota from unknown quota.
Providers expose different metrics and scopes; it never invents a remaining-
quota percentage when an API does not publish one.
"""
from __future__ import annotations

import hashlib
import re
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests


class QuotaAwareRouter:
    """Rank provider routes using live quota telemetry and observed failures."""

    def __init__(self, probe_ttl_seconds: float = 60.0, timeout: float = 10.0):
        self.probe_ttl_seconds = max(5.0, float(probe_ttl_seconds))
        self.timeout = max(2.0, float(timeout))
        self.pools: Dict[str, Dict[str, Any]] = {}
        self.credentials: Dict[str, Dict[str, Any]] = {}
        self.route_usage: Dict[str, Dict[str, Any]] = {}
        self._last_summary_at = 0.0

    @staticmethod
    def fingerprint(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _duration_seconds(value: Any) -> Optional[float]:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            pass
        # Groq-style durations: 2m59.56s, 7.66s, 1h2m.
        matches = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", raw.lower())
        if matches:
            factors = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
            return sum(float(amount) * factors[unit] for amount, unit in matches)
        return None

    def _pool(self, scope: str) -> Dict[str, Any]:
        return self.pools.setdefault(scope, {
            "remaining_ratio": None,
            "quota_metric": None,
            "quota_detail": "not yet observed",
            "cooldown_until": 0.0,
            "exhausted": False,
            "last_observed": 0.0,
            "last_status": None,
            "preferred_credential_id": None,
            "applies_to_all_routes": True,
            "successes": 0,
            "failures": 0,
        })

    def _credential(self, credential_id: str) -> Dict[str, Any]:
        return self.credentials.setdefault(credential_id, {
            "disabled": False,
            "reason": "",
            "last_probe": 0.0,
            "probe_ok": None,
        })

    def _set_cooldown(
        self,
        scope: str,
        seconds: float,
        reason: str,
        applies_to_all_routes: bool = True,
    ) -> None:
        pool = self._pool(scope)
        seconds = max(1.0, min(86400.0, float(seconds)))
        pool["cooldown_until"] = max(pool["cooldown_until"], time.monotonic() + seconds)
        pool["quota_detail"] = reason
        pool["exhausted"] = False
        pool["applies_to_all_routes"] = bool(applies_to_all_routes)

    @staticmethod
    def _response_json(response: Any) -> Dict[str, Any]:
        try:
            value = response.json()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _openrouter_limit_source(payload: Dict[str, Any]) -> str:
        error = payload.get("error") if isinstance(payload, dict) else None
        metadata = error.get("metadata") if isinstance(error, dict) else None
        source = metadata.get("limit_source") if isinstance(metadata, dict) else None
        return str(source or "").strip().lower()

    @staticmethod
    def _mark_exhausted(pool: Dict[str, Any], reason: str, applies_to_all_routes: bool = True) -> None:
        pool["exhausted"] = True
        pool["remaining_ratio"] = 0.0
        pool["quota_detail"] = reason
        pool["applies_to_all_routes"] = bool(applies_to_all_routes)

    def observe_response(
        self,
        provider: str,
        scope: str,
        response: Any,
        credential_id: Optional[str] = None,
        shared_scope: Optional[str] = None,
        model_scope: Optional[str] = None,
    ) -> None:
        """Record quota headers/status against their actual provider scope."""
        provider_name = str(provider or "").lower()
        pool = self._pool(scope)
        status = int(getattr(response, "status_code", 0) or 0)
        headers = {str(k).lower(): str(v) for k, v in (getattr(response, "headers", {}) or {}).items()}
        payload = self._response_json(response) if provider_name == "openrouter" else {}
        limit_source = self._openrouter_limit_source(payload) if provider_name == "openrouter" else ""
        pool["last_observed"] = time.monotonic()
        pool["last_status"] = status

        if status and 200 <= status < 300:
            pool["successes"] += 1
            pool["exhausted"] = False
            pool["cooldown_until"] = 0.0
            if credential_id:
                pool["preferred_credential_id"] = credential_id

        if provider_name == "groq":
            ratios = []
            labels = []
            for label, remaining_name, limit_name in (
                ("RPD", "x-ratelimit-remaining-requests", "x-ratelimit-limit-requests"),
                ("TPM", "x-ratelimit-remaining-tokens", "x-ratelimit-limit-tokens"),
            ):
                try:
                    remaining = float(headers[remaining_name])
                    limit = float(headers[limit_name])
                    if limit > 0:
                        ratio = max(0.0, min(1.0, remaining / limit))
                        ratios.append(ratio)
                        labels.append(f"{label} {remaining:g}/{limit:g}")
                except (KeyError, TypeError, ValueError):
                    continue
            if ratios:
                pool["remaining_ratio"] = min(ratios)
                pool["quota_metric"] = "Groq rate-limit headers"
                pool["quota_detail"] = ", ".join(labels)
                if pool["remaining_ratio"] <= 0:
                    delays = [
                        self._duration_seconds(headers.get("x-ratelimit-reset-requests")),
                        self._duration_seconds(headers.get("x-ratelimit-reset-tokens")),
                    ]
                    positive = [d for d in delays if d is not None and d > 0]
                    self._set_cooldown(scope, max(positive) if positive else 60, "Groq limit exhausted; waiting for reset")

        if status == 429:
            body = str(getattr(response, "text", "") or "")
            retry_hint = None
            match = re.search(r"retryDelay[\"']?\s*[:=]\s*[\"']([^\"']+)", body, re.I)
            if match:
                retry_hint = self._duration_seconds(match.group(1))
            if retry_hint is None:
                match = re.search(r"retry in\s+([0-9.]+)\s*s", body, re.I)
                if match:
                    retry_hint = float(match.group(1))
            explicit_retry_delay = self._duration_seconds(headers.get("retry-after")) or retry_hint
            delay = explicit_retry_delay or 60.0

            if provider_name == "groq":
                # Groq exposes reset durations in response headers. Prefer those
                # measured windows over a generic fallback delay so the router
                # does not unnecessarily park a whole organization for 60s.
                reset_delays = [
                    self._duration_seconds(headers.get("x-ratelimit-reset-requests")),
                    self._duration_seconds(headers.get("x-ratelimit-reset-tokens")),
                ]
                useful_delays = [value for value in reset_delays if value is not None and value > 0]
                groq_delay = max(useful_delays) if useful_delays else (explicit_retry_delay or 60.0)
                self._set_cooldown(scope, groq_delay, f"Groq rate limit reached; retry after about {groq_delay:.0f}s")
            elif provider_name == "openrouter" and limit_source in {
                "openrouter_free_tier_per_minute",
                "openrouter_free_tier_daily",
            }:
                target = shared_scope or scope
                if limit_source == "openrouter_free_tier_daily":
                    # Daily free-model caps apply to the account's entire free-model
                    # pool. Keep that pool unavailable for the current workflow run;
                    # a fresh run will re-check the account instead of guessing reset.
                    shared_pool = self._pool(target)
                    self._mark_exhausted(
                        shared_pool,
                        "OpenRouter free-model daily account quota exhausted for this run",
                        applies_to_all_routes=False,
                    )
                else:
                    self._set_cooldown(
                        target,
                        delay,
                        f"OpenRouter free-model account RPM exhausted; retry after about {delay:.0f}s",
                        applies_to_all_routes=False,
                    )
            elif provider_name == "openrouter" and limit_source == "upstream_provider_shared_pool":
                # OpenRouter's upstream shared-pool throttle is model/provider-path
                # specific, not a reason to block sibling models or the whole account.
                target = model_scope or scope
                self._set_cooldown(
                    target,
                    delay,
                    f"OpenRouter upstream shared pool throttled; retry after about {delay:.0f}s",
                    applies_to_all_routes=True,
                )
            elif provider_name == "openrouter" and limit_source == "openrouter_in_flight_budget":
                target = shared_scope or scope
                self._set_cooldown(
                    target,
                    self._duration_seconds(headers.get("retry-after")) or 10.0,
                    "OpenRouter account in-flight budget reached",
                    applies_to_all_routes=True,
                )
            else:
                # Generic 429s are conservatively scoped to the provider's real
                # quota pool (organization, project, or account where known).
                self._set_cooldown(scope, delay, f"HTTP 429; retry after about {delay:.0f}s")
            pool["failures"] += 1
        elif status == 402:
            if provider_name == "openrouter" and limit_source == "openrouter_credits":
                target = shared_scope or scope
                self._mark_exhausted(self._pool(target), "OpenRouter account credits exhausted", applies_to_all_routes=True)
            elif provider_name == "openrouter" and limit_source == "openrouter_key_limit":
                self._mark_exhausted(pool, "OpenRouter API-key spending limit exhausted")
            elif provider_name == "openrouter" and limit_source == "openrouter_in_flight_budget":
                target = shared_scope or scope
                self._set_cooldown(target, self._duration_seconds(headers.get("retry-after")) or 10.0,
                                   "OpenRouter account in-flight budget reached", applies_to_all_routes=True)
            else:
                self._mark_exhausted(pool, "payment/credit/quota exhausted (HTTP 402)")
            pool["failures"] += 1
        elif status in (401, 403) and credential_id:
            credential = self._credential(credential_id)
            credential["disabled"] = True
            credential["reason"] = f"HTTP {status}; credential unauthorized"
            pool["failures"] += 1
        elif status >= 500:
            delay = self._duration_seconds(headers.get("retry-after")) or 20.0
            self._set_cooldown(scope, delay, f"temporary provider error HTTP {status}")
            pool["failures"] += 1
        elif status >= 400:
            pool["failures"] += 1

    def mark_error(
        self,
        provider: str,
        scope: str,
        credential_id: Optional[str],
        status_code: Optional[int],
        detail: str = "",
    ) -> None:
        """Record failures raised before a response object can be retained."""
        pool = self._pool(scope)
        status = int(status_code or 0)
        pool["last_status"] = status or None
        pool["last_observed"] = time.monotonic()
        pool["failures"] += 1
        if status == 429:
            delay = self._duration_seconds(detail) or 60.0
            self._set_cooldown(scope, delay, f"HTTP 429; retry after about {delay:.0f}s")
        elif status == 402:
            pool["exhausted"] = True
            pool["remaining_ratio"] = 0.0
            pool["quota_detail"] = "payment/credit/quota exhausted (HTTP 402)"
        elif status in (401, 403) and credential_id:
            credential = self._credential(credential_id)
            credential["disabled"] = True
            credential["reason"] = f"HTTP {status}; credential unauthorized"
        elif status >= 500:
            delay = self._duration_seconds(detail) or 20.0
            self._set_cooldown(scope, delay, f"temporary provider error HTTP {status}")

    def _probe_openrouter(self, route: Dict[str, Any]) -> None:
        """OpenRouter exposes current-key status and remaining key spend limit."""
        api_key = route.get("probe_key")
        credential_id = route.get("credential_id")
        scope = route.get("scope")
        if not api_key or not credential_id or not scope:
            return
        credential = self._credential(credential_id)
        now = time.monotonic()
        last_probe = float(credential.get("last_probe", 0.0) or 0.0)
        # A fresh GitHub Actions runner can have a monotonic clock below the
        # probe TTL. Treat 0 as "never probed", not as a recent probe.
        if last_probe > 0.0 and now - last_probe < self.probe_ttl_seconds:
            return
        credential["last_probe"] = now
        try:
            response = requests.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                self.observe_response(
                    "OpenRouter", scope, response, credential_id,
                    shared_scope=route.get("shared_scope"),
                    model_scope=route.get("model_scope"),
                )
                credential["probe_ok"] = False
                return
            data = (response.json() or {}).get("data") or {}
            credential["probe_ok"] = True
            credential["is_free_tier"] = bool(data.get("is_free_tier"))
            limit = data.get("limit")
            remaining = data.get("limit_remaining")
            pool = self._pool(scope)
            if limit is not None and remaining is not None:
                try:
                    limit_value = float(limit)
                    remaining_value = float(remaining)
                    if limit_value > 0:
                        ratio = max(0.0, min(1.0, remaining_value / limit_value))
                        pool["remaining_ratio"] = ratio
                        pool["quota_metric"] = "OpenRouter key spend limit"
                        pool["quota_detail"] = f"${remaining_value:.4g} of ${limit_value:.4g} remaining"
                        pool["exhausted"] = ratio <= 0
                except (TypeError, ValueError):
                    pass
            else:
                # The endpoint does not disclose free-tier request/token quota.
                pool["remaining_ratio"] = None
                pool["quota_metric"] = None
                if data.get("is_free_tier"):
                    pool["quota_detail"] = "key valid; free-tier request quota not exposed by this endpoint"
                else:
                    pool["quota_detail"] = "key valid; no finite key spending limit reported"
        except requests.RequestException as exc:
            credential["probe_ok"] = None
            self._pool(scope)["quota_detail"] = f"preflight temporarily unavailable: {type(exc).__name__}"
        except (ValueError, TypeError) as exc:
            credential["probe_ok"] = None
            self._pool(scope)["quota_detail"] = f"preflight response unreadable: {type(exc).__name__}"

    def invalidate_probe(self, credential_id: str) -> None:
        """Force the next OpenRouter ranking pass to refresh post-request usage."""
        if credential_id in self.credentials:
            self.credentials[credential_id]["last_probe"] = 0.0

    def route_available(self, route: Dict[str, Any]) -> bool:
        scope = str(route.get("scope") or "")
        credential_id = str(route.get("credential_id") or "")
        pool = self._pool(scope)
        credential = self._credential(credential_id) if credential_id else {"disabled": False}
        if credential.get("disabled") or pool.get("exhausted"):
            return False
        now = time.monotonic()
        if float(pool.get("cooldown_until", 0.0)) > now:
            return False

        shared_scope = str(route.get("shared_scope") or "")
        if shared_scope:
            shared_pool = self._pool(shared_scope)
            # Free-model request caps do not block paid OpenRouter model routes.
            applies = shared_pool.get("applies_to_all_routes", True) or bool(route.get("uses_free_model"))
            if applies and (shared_pool.get("exhausted") or float(shared_pool.get("cooldown_until", 0.0)) > now):
                return False

        model_scope = str(route.get("model_scope") or "")
        if model_scope:
            model_pool = self._pool(model_scope)
            if model_pool.get("exhausted") or float(model_pool.get("cooldown_until", 0.0)) > now:
                return False
        return True

    def mark_selected(self, route: Dict[str, Any]) -> None:
        route_id = str(route.get("id") or route.get("name") or "unknown")
        state = self.route_usage.setdefault(route_id, {"count": 0, "last_selected": 0.0})
        state["count"] += 1
        state["last_selected"] = time.monotonic()

    def rank_routes(self, routes: Iterable[Dict[str, Any]], log: bool = True) -> List[Dict[str, Any]]:
        """Preflight supported sources, then rank by known remaining capacity.

        Unknown sources receive a neutral score (0.50) and are balanced by
        least-used/least-recently-used routing. This is intentionally not
        presented as a measured quota value.
        """
        routes = list(routes)
        for route in routes:
            if str(route.get("provider", "")).lower() == "openrouter":
                self._probe_openrouter(route)

        candidates = []
        summary = []
        now = time.monotonic()
        for index, route in enumerate(routes):
            scope = str(route.get("scope") or "")
            credential_id = str(route.get("credential_id") or "")
            pool = self._pool(scope)
            credential = self._credential(credential_id) if credential_id else {"disabled": False}
            if not self.route_available(route):
                continue
            ratio = pool.get("remaining_ratio")
            score = float(ratio) if ratio is not None else 0.50
            usage = self.route_usage.get(str(route.get("id") or route.get("name") or "unknown"), {})
            count = int(usage.get("count", 0))
            last = float(usage.get("last_selected", 0.0))
            preferred = 1 if credential_id and pool.get("preferred_credential_id") == credential_id else 0
            tie_hash = self.fingerprint(str(route.get("id") or index))
            candidates.append((score, preferred, -count, -last, tie_hash, route))
            if log:
                label = pool.get("quota_detail") or "quota not exposed before request"
                if ratio is not None:
                    summary.append(f"{route.get('name')}: {ratio * 100:.1f}% measured ({label})")
                else:
                    summary.append(f"{route.get('name')}: UNKNOWN (neutral scheduling; {label})")

        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
        ranked = [item[5] for item in candidates]
        if log and summary:
            print("Quota-aware routing snapshot:")
            for line in summary:
                print(f"  - {line}")
            print(f"  Selected next: {ranked[0].get('name')}" if ranked else "  No route currently available.")
        return ranked
