
"""Bounded multi-provider AI routing for text planning and image verification.

Keys are read only from environment variables. Provider failures never expose
request headers or API keys in messages. A genuine visual REJECT is a normal
verdict and does not trigger provider failover.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


class ProviderRoutingError(RuntimeError):
    """No configured AI provider could complete the requested operation."""


class VisionServiceUnavailable(ProviderRoutingError):
    """All visual-verification routes failed at the service/transport layer."""


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    api_key: str
    endpoint: str
    kind: str

    @property
    def route_id(self) -> str:
        return f"{self.provider}:{self.model}"


# Provider health is isolated by task. A text-generation outage must not put
# the same provider/model on cooldown for image verification (and vice versa).
_DISABLED_PROVIDERS: set[tuple[str, str]] = set()
_DISABLED_ROUTES: set[tuple[str, str]] = set()
_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}
_PROVIDER_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}

# A 429 is usually a short-window rate limit, not a dead provider. Disabling the
# provider for the whole run used to remove an otherwise healthy fallback route
# for narration, fact-checking and vision. Exhausted quota/billing stays fatal
# for the run because it cannot recover on its own.
_QUOTA_FAILURE_MARKERS = (
    "quota", "billing", "insufficient", "credit", "payment", "daily limit",
    "exceeded your current",
)


def _positive_env_seconds(name: str, default: int) -> float:
    try:
        value = float(_env(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if not value or value <= 0:
        value = float(default)
    return max(15.0, min(value, 3600.0))


def _transient_cooldown_seconds() -> float:
    return _positive_env_seconds("AI_TRANSIENT_COOLDOWN_SECONDS", 300)


def _rate_limit_cooldown_seconds() -> float:
    return _positive_env_seconds("AI_RATE_LIMIT_COOLDOWN_SECONDS", 180)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _order(name: str, default: str) -> list[str]:
    values = [x.strip().lower() for x in _env(name, default).split(",")]
    return list(dict.fromkeys(x for x in values if x))


# --- Free-tier policy -------------------------------------------------------
# The pipeline is expected to run without spending money. DeepSeek is the only
# route here that draws on a prepaid balance, and OpenRouter also serves paid
# model slugs, so both stay out of the run unless an operator explicitly opts
# in. Free-tier providers (Gemini AI Studio, Groq Free plan, OpenRouter ":free"
# models) are always allowed.
_PAID_PROVIDERS = {"deepseek"}
_FREE_ONLY_WARNED: set[str] = set()


def paid_providers_allowed() -> bool:
    """True only when the operator explicitly accepts possible charges."""
    return _env("AI_ALLOW_PAID_PROVIDERS", "false").lower() in {
        "1", "true", "yes", "on",
    }


def free_tier_only() -> bool:
    """True when this process may only use zero-cost provider routes."""
    return not paid_providers_allowed()


def _is_free_model(model: str) -> bool:
    """Whether an OpenRouter slug is billed at $0."""
    slug = str(model or "").strip().lower()
    return slug == "openrouter/free" or slug.endswith(":free")


def _skip_paid_route(provider: str, model: str) -> None:
    label = f"{provider}/{model}"
    if label in _FREE_ONLY_WARNED:
        return
    _FREE_ONLY_WARNED.add(label)
    print(
        f"Free-tier mode: {label} is not used because it can incur charges. "
        "Set AI_ALLOW_PAID_PROVIDERS=true only if paid usage is acceptable."
    )


def _vision_min_confidence() -> float:
    """Keep the production Vision floor at 0.72; configuration may only make it stricter."""
    try:
        requested = float(_env("FOOTAGE_VISION_MIN_CONFIDENCE", "0.72"))
    except (TypeError, ValueError):
        requested = 0.72
    return max(0.72, min(1.0, requested))


def configured_vision_provider_names() -> list[str]:
    names = []
    for route in _routes("vision"):
        if route.provider == "openrouter":
            label = f"OpenRouter ({route.model})"
        else:
            label = f"{route.provider.title()} ({route.model})"
        if label not in names:
            names.append(label)
    return names


def _routes(task: str) -> list[Route]:
    if task == "text":
        order = _order("AI_TEXT_PROVIDER_ORDER", "gemini,groq,openrouter")
    elif task == "fact":
        # Keep fact review on an independent provider order/cooldown from script writing.
        order = _order("AI_FACT_PROVIDER_ORDER", "gemini,groq,openrouter")
    else:
        order = _order("AI_VISION_PROVIDER_ORDER", "gemini,groq,openrouter")

    paid_allowed = paid_providers_allowed()
    routes: list[Route] = []
    for provider in order:
        if provider in _PAID_PROVIDERS and not paid_allowed:
            _skip_paid_route(provider, "configured")
            continue
        if provider == "groq":
            key = _env("GROQ_API_KEY")
            model = _env(
                "GROQ_VISION_MODEL" if task == "vision" else "GROQ_MODEL",
                "openai/gpt-oss-120b" if task in ("text", "fact") else "qwen/qwen3.8-27b",
            )
            if key and model:
                routes.append(Route(
                    "groq", model, key,
                    "https://api.groq.com/openai/v1/chat/completions",
                    "openai",
                ))
        elif provider == "deepseek":
            key = _env("DEEPSEEK_API_KEY")
            if task == "vision":
                # DeepSeek's chat endpoint is text-only. Routing frames here
                # always failed with a request error and silently consumed one
                # of the bounded vision attempts, so vision is opt-in only when
                # an operator knows a multimodal DeepSeek model is available.
                model = _env("DEEPSEEK_VISION_MODEL")
            else:
                model = _env("DEEPSEEK_MODEL", "deepseek-flash")
            if key and model:
                routes.append(Route(
                    "deepseek", model, key,
                    "https://api.deepseek.com/chat/completions",
                    "openai",
                ))
        elif provider == "gemini":
            key = _env("GEMINI_API_KEY")
            model = _env(
                "GEMINI_VISION_MODEL" if task == "vision" else "GEMINI_MODEL",
                "gemini-3.8-flash",
            )
            if key and model:
                routes.append(Route(
                    "gemini", model, key,
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    "gemini",
                ))
        elif provider == "openrouter":
            key = _env("OPENROUTER_API_KEY")
            model = _env(
                "OPENROUTER_VISION_MODEL" if task == "vision" else "OPENROUTER_MODEL",
                "openrouter/free" if task in ("text", "fact") else "nvidia/nemotron-3.5-super-vl:free",
            )
            if key and model:
                routes.append(Route(
                    "openrouter", model, key,
                    "https://openrouter.ai/api/v1/chat/completions",
                    "openai",
                ))
                if task in ("text", "fact"):
                    # The dynamic free router can select a reasoning model that
                    # exhausts its output budget before returning JSON. Keep an
                    # explicit structured-output route as a second option, using
                    # the same existing OpenRouter API key.
                    fallback = _env(
                        "OPENROUTER_TEXT_FALLBACK_MODEL",
                        "baidu/ernie-5.5-flash-preview:free",
                    )
                    if fallback and fallback != model:
                        routes.append(Route(
                            "openrouter", fallback, key,
                            "https://openrouter.ai/api/v1/chat/completions",
                            "openai",
                        ))
                if task == "vision":
                    fallback = _env(
                        "OPENROUTER_VISION_FALLBACK_MODEL",
                        "inclusionai/ling-3.2-flash:free",
                    )
                    if fallback and fallback != model:
                        routes.append(Route(
                            "openrouter", fallback, key,
                            "https://openrouter.ai/api/v1/chat/completions",
                            "openai",
                        ))
    if not paid_allowed:
        # OpenRouter is filtered by slug as well, because a paid slug under the
        # same key would silently bill the account.
        allowed: list[Route] = []
        for route in routes:
            if route.provider == "openrouter" and not _is_free_model(route.model):
                _skip_paid_route(route.provider, route.model)
                continue
            allowed.append(route)
        routes = allowed

    return routes


def _safe_detail(response: requests.Response) -> str:
    """Return a short diagnostic without echoing credentials or full payloads."""
    try:
        body = response.json()
    except Exception:
        body = {}
    if isinstance(body, dict):
        error = body.get("error", {})
        if isinstance(error, dict):
            message = str(error.get("message", ""))
        else:
            message = str(error)
    else:
        message = ""
    message = re.sub(r"\s+", " ", message).strip()
    message = re.sub(
        r"(?i)(key|token|authorization)\s*[:=]\s*[^ ,;]+",
        r"\1=[REDACTED]",
        message,
    )
    return message[:240] or f"HTTP {response.status_code}"


def _mark_failure(
    task: str,
    route: Route,
    status: int | None,
    detail: str,
    retry_after: float | None = None,
) -> None:
    detail = str(detail or "").replace(route.api_key, "[REDACTED]")
    detail = re.sub(r"(?i)Bearer\s+[^ ,;]+", "Bearer [REDACTED]", detail)

    provider_key = (task, route.provider)
    route_key = (task, route.route_id)

    if status == 429:
        lowered = detail.lower()
        exhausted = any(marker in lowered for marker in _QUOTA_FAILURE_MARKERS)
        if exhausted:
            _DISABLED_PROVIDERS.add(provider_key)
            print(
                f"AI router: {route.provider} reports an exhausted quota or "
                "billing limit (HTTP 429); provider skipped for this task for "
                "the rest of this run."
            )
        else:
            cooldown = min(
                900.0,
                max(15.0, retry_after or _rate_limit_cooldown_seconds()),
            )
            _PROVIDER_COOLDOWN_UNTIL[provider_key] = time.monotonic() + cooldown
            _COOLDOWN_UNTIL[route_key] = time.monotonic() + cooldown
            print(
                f"AI router: {route.provider} rate limited (HTTP 429); provider "
                f"paused for {cooldown:.0f}s and the next route is used now."
            )
    elif status in (401, 403):
        _DISABLED_PROVIDERS.add(provider_key)
        print(
            f"AI router: {route.provider} authentication/permission error "
            f"(HTTP {status}); provider disabled for this task for this run."
        )
    elif status == 402:
        _DISABLED_PROVIDERS.add(provider_key)
        print(
            f"AI router: {route.provider} reports insufficient balance/payment "
            "(HTTP 402); provider skipped for this task for the rest of this run. "
            "Check the provider account balance or billing status."
        )
    elif status in (400, 404, 413, 415, 422):
        _DISABLED_ROUTES.add(route_key)
        print(
            f"AI router: route {route.route_id} rejected configuration/request "
            f"(HTTP {status}); trying another route. {detail}"
        )
    elif status is not None or detail:
        cooldown = _transient_cooldown_seconds()
        _COOLDOWN_UNTIL[route_key] = time.monotonic() + cooldown
        print(
            f"AI router: {route.route_id} temporarily unavailable; "
            f"switching provider. {detail}"
        )
    else:
        _COOLDOWN_UNTIL[route_key] = (
            time.monotonic() + _transient_cooldown_seconds()
        )
        print(
            f"AI router: {route.route_id} connection timeout/network error; "
            "switching provider."
        )


def _usable(task: str, route: Route) -> bool:
    provider_key = (task, route.provider)
    route_key = (task, route.route_id)
    if provider_key in _DISABLED_PROVIDERS or route_key in _DISABLED_ROUTES:
        return False
    now = time.monotonic()
    if _COOLDOWN_UNTIL.get(route_key, 0) > now:
        return False
    if _PROVIDER_COOLDOWN_UNTIL.get(provider_key, 0) > now:
        return False
    return True


def _extract_json(raw: str) -> Any:
    raw = str(raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start < 0:
            raise ValueError("AI response did not contain a JSON object")

        depth = 0
        in_string = False
        escaped = False

        for index in range(start, len(raw)):
            char = raw[index]

            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(raw[start:index + 1])

        raise ValueError("AI response contained incomplete JSON")


def _openai_request(
    route: Route,
    prompt: str,
    images: list[bytes] | None,
    *,
    task: str,
) -> str:
    headers = {
        "Authorization": f"Bearer {route.api_key}",
        "Content-Type": "application/json",
    }

    if route.provider == "openrouter":
        headers["HTTP-Referer"] = _env(
            "OPENROUTER_SITE_URL",
            "https://github.com/",
        )
        headers["X-Title"] = "Auto Documentary Video"

    if images:
        user_content: Any = [{"type": "text", "text": prompt}]
        for image_bytes in images:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{encoded}"
                },
            })
    else:
        user_content = prompt

    system = (
        "You are a precise documentary production assistant. "
        "Return only one valid JSON object."
        if task in ("text", "fact")
        else
        "You are a strict visual footage verifier. Inspect the pixels, "
        "not just metadata. Return only JSON with boolean relevant, "
        "numeric confidence, and brief reason."
    )

    payload: dict[str, Any] = {
        "model": route.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.25 if task in ("text", "fact") else 0.05,
    }

    if task in ("text", "fact"):
        # Groq's free plan counts the completion budget against a 8,000 TPM
        # ceiling, so a 7,000-token budget plus the planner prompt was rejected
        # with HTTP 413 before the model ever ran. The documentary plan needs
        # roughly 1,500 output tokens, so 3,000 keeps real headroom.
        payload["max_tokens"] = 7000

        # All OpenAI-compatible text routes used by this project support JSON
        # mode. Asking for it is particularly important for OpenRouter's free
        # router: it can then select only models advertising structured-output
        # support rather than relying on a prose-only instruction.
        # Groq validates json_object output server-side and answers HTTP 400
        # "Failed to validate JSON" when a reasoning model truncates its JSON,
        # which hides a repairable parse error behind a provider failure. The
        # router parses and repairs structured output itself, so Groq is asked
        # for low reasoning effort and no server-side JSON validation.
        if route.provider in ("deepseek", "openrouter"):
            payload["response_format"] = {"type": "json_object"}
        if route.provider == "groq":
            payload["reasoning_effort"] = "low"

        if route.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}

        if route.provider == "openrouter":
            # The free router can select a reasoning model that spends the whole
            # completion budget thinking and returns empty content with
            # finish_reason=length. Low effort reserves that budget for the
            # required structured JSON.
            payload["reasoning"] = {"effort": "low"}

        if route.provider == "groq":
            # The free plan counts prompt plus completion budget against an
            # 8,000 tokens-per-minute ceiling. A fixed large budget was rejected
            # with HTTP 413, while a too-small budget truncated the JSON and
            # Groq's json_object mode answered HTTP 400 "Failed to validate
            # JSON". Size the budget from the actual prompt instead.
            payload.pop("max_tokens", None)
            approx_prompt_tokens = max(1, len(prompt) // 4)
            payload["max_completion_tokens"] = min(
                6000, max(1500, 8000 - approx_prompt_tokens - 400)
            )
    else:
        payload["max_tokens"] = 600 if route.provider != "groq" else 500

        if route.provider == "groq":
            payload.pop("max_tokens", None)
            payload["max_completion_tokens"] = 500

        if route.provider in ("deepseek", "groq", "openrouter"):
            payload["response_format"] = {"type": "json_object"}

        if route.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}

    response = requests.post(
        route.endpoint,
        headers=headers,
        json=payload,
        timeout=(12, 50 if task == "vision" else 120),
    )

    if response.status_code >= 400:
        raise _HTTPFailure(
            response.status_code, _safe_detail(response), _retry_after_seconds(response)
        )

    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        content_type = ""
        try:
            content_type = str(response.headers.get("Content-Type", ""))[:80]
        except Exception:
            pass
        raise ValueError(
            "response body was not JSON "
            f"(HTTP {response.status_code}, content-type={content_type or 'unknown'})"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError("provider response root is not a JSON object")

    if data.get("error"):
        detail = str(data.get("error"))[:240]
        detail = detail.replace(route.api_key, "[REDACTED]")
        raise ValueError(f"provider returned an error object: {detail}")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError(
            "provider response has no chat-completion choices "
            f"(top-level keys={','.join(sorted(data.keys())[:12])})"
        )

    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("first provider choice has no assistant message object")

    content = message.get("content")
    if content is None and isinstance(message.get("parsed"), dict):
        content = json.dumps(message["parsed"], ensure_ascii=False)

    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                text_value = item.get("text")
                if text_value is None and isinstance(item.get("content"), str):
                    text_value = item["content"]
                if text_value:
                    text_parts.append(str(text_value))
        content = "\n".join(text_parts)
    elif isinstance(content, dict):
        content = content.get("text", "")

    content = str(content or "").strip()
    if not content:
        finish_reason = str(choices[0].get("finish_reason", "unknown"))[:60]
        message_keys = ",".join(sorted(message.keys())[:12])
        raise ValueError(
            "provider returned empty assistant content "
            f"(finish_reason={finish_reason}, message_keys={message_keys})"
        )

    return content


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Read a numeric Retry-After header, ignoring HTTP-date formats."""
    try:
        raw = str(response.headers.get("Retry-After", "") or "").strip()
    except Exception:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return min(value, 900.0)


class _HTTPFailure(Exception):
    def __init__(self, status: int, detail: str, retry_after: float | None = None):
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: {detail}")


_GEMINI_WORKING_MODEL: dict[str, str] = {}


def _gemini_model_candidates(preferred: str) -> list[str]:
    """Free-tier Gemini models to try, most preferred first."""
    fallbacks = [
        item.strip()
        for item in _env(
            "GEMINI_MODEL_FALLBACKS",
            "gemini-3.5-flash-lite,gemini-3.8-flash,gemini-2.5-flash,gemini-2.0-flash",
        ).split(",")
        if item.strip()
    ]
    preferred = str(preferred or "").strip()
    models = ([preferred] if preferred else []) + fallbacks
    return list(dict.fromkeys(models)) or ["gemini-3.8-flash"]


def _gemini_request(
    route: Route,
    prompt: str,
    images: list[bytes] | None,
    *,
    task: str,
) -> str:
    parts: list[dict[str, Any]] = [{"text": prompt}]

    for image_bytes in (images or [])[:2]:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }
        })

    payload: dict[str, Any] = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.25 if task in ("text", "fact") else 0.05,
            "maxOutputTokens": 7000 if task in ("text", "fact") else 600,
        },
    }

    headers = {
        "x-goog-api-key": route.api_key,
        "Content-Type": "application/json",
    }
    timeout = (12, 50 if task == "vision" else 120)

    # Gemini model IDs change between releases. A model name that this key no
    # longer serves answers 404, which used to end the whole run. Try the next
    # configured free model instead, and remember the one that worked.
    models = _gemini_model_candidates(_GEMINI_WORKING_MODEL.get(task) or route.model)
    response = None
    for index, model in enumerate(models):
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        response = requests.post(
            endpoint, headers=headers, json=payload, timeout=timeout
        )
        if response.status_code != 404 or index == len(models) - 1:
            if response.status_code < 400:
                _GEMINI_WORKING_MODEL[task] = model
            break
        print(
            f"Gemini model {model} is not available for this key; "
            f"trying {models[index + 1]}."
        )

    assert response is not None

    if response.status_code >= 400:
        raise _HTTPFailure(
            response.status_code, _safe_detail(response), _retry_after_seconds(response)
        )

    try:
        data = response.json()
        candidates = data.get("candidates") or []
        content = candidates[0]["content"]["parts"]
        raw = "\n".join(
            str(part.get("text", ""))
            for part in content
            if part.get("text")
        )

        if not raw.strip():
            raise ValueError("empty Gemini response")

        return raw.strip()

    except Exception as exc:
        raise ValueError(
            f"Unreadable Gemini response ({type(exc).__name__})"
        ) from exc


def _request(
    route: Route,
    prompt: str,
    images: list[bytes] | None,
    task: str,
) -> str:
    if route.kind == "gemini":
        return _gemini_request(route, prompt, images, task=task)
    return _openai_request(route, prompt, images, task=task)


def _repair_prompt(original_prompt: str, error: Exception) -> str:
    """Ask the same provider once to repair a malformed/invalid structured reply."""
    detail = re.sub(r"\s+", " ", str(error or "invalid response")).strip()
    detail = detail[:600] or "invalid response"
    return (
        f"{original_prompt}\n\n"
        "STRICT OUTPUT REPAIR REQUIRED: Your previous response was rejected by "
        "the application's JSON parser or validation rules. Regenerate the "
        "ENTIRE response from scratch, preserving the required schema and all "
        "requirements. Fix this exact validation issue: "
        f"{detail}. Return only one complete valid JSON object. Do not add "
        "Markdown fences, commentary, or explanations."
    )


def _route(
    task: str,
    prompt: str,
    *,
    images: list[bytes] | None = None,
    validator: Callable[[Any], Any] | None = None,
    _transient_retry: bool = False,
    _prior_errors: list[str] | None = None,
) -> tuple[Any, str, str]:
    errors: list[str] = list(_prior_errors or [])
    transient_routes: list[Route] = []
    routes = _routes(task)

    for route in routes:
        if not _usable(task, route):
            continue

        print(
            f"AI router: trying {route.provider} / {route.model} "
            f"for {task}..."
        )

        route_images = images
        if images and route.provider == "groq":
            route_images = [images[len(images) // 2]]
        elif images:
            route_images = [images[0], images[-1]]

        # A semantically invalid but parseable response is not proof that the
        # provider is unavailable. Retry that same route once with the exact
        # validation error before consuming another provider's quota.
        request_prompt = prompt
        repair_used = False

        while True:
            try:
                raw = _request(route, request_prompt, route_images, task)
                value: Any = _extract_json(raw)

                if validator:
                    value = validator(value)

                if task == "vision":
                    if not isinstance(value, dict):
                        raise ValueError("vision response is not a JSON object")

                    relevant = value.get("relevant")
                    confidence = value.get("confidence")

                    if not isinstance(relevant, bool) or isinstance(confidence, bool):
                        raise ValueError(
                            "vision response needs boolean relevant and numeric confidence"
                        )

                    confidence = float(confidence)
                    if not 0 <= confidence <= 1:
                        raise ValueError(
                            "vision confidence must be between 0 and 1"
                        )

                    reason = re.sub(
                        r"\s+",
                        " ",
                        str(value.get("reason", "No reason provided")),
                    ).strip()[:400]

                    result = {
                        "relevant": relevant,
                        "confidence": confidence,
                        "reason": reason,
                        "accepted": (
                            relevant
                            and confidence >= _vision_min_confidence()
                        ),
                        "provider": route.provider,
                        "model": route.model,
                    }

                    print(
                        f"AI router: {route.provider}/{route.model} "
                        f"visual verdict={'PASS' if result['accepted'] else 'REJECT'} "
                        f"({confidence:.2f})."
                    )
                    return result, route.provider, route.model

                print(
                    f"AI router: {route.provider}/{route.model} "
                    f"returned a valid {task} response."
                )
                return value, route.provider, route.model

            except _HTTPFailure as exc:
                errors.append(
                    f"{route.provider}/{route.model}: HTTP {exc.status}"
                )
                _mark_failure(task, route, exc.status, exc.detail, exc.retry_after)
                if exc.status in (408, 500, 502, 503, 504):
                    transient_routes.append(route)
                break

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as exc:
                errors.append(
                    f"{route.provider}/{route.model}: {type(exc).__name__}"
                )
                _mark_failure(task, route, None, "network timeout/connection error")
                transient_routes.append(route)
                break

            except requests.exceptions.RequestException as exc:
                errors.append(
                    f"{route.provider}/{route.model}: {type(exc).__name__}"
                )
                _mark_failure(task, route, None, "request transport error")
                transient_routes.append(route)
                break

            except (ValueError, KeyError, TypeError) as exc:
                if task in ("text", "fact") and not repair_used:
                    repair_used = True
                    if route.provider == "groq":
                        # The first request already consumed most of Groq's
                        # 8,000 tokens-per-minute free-plan window, so an
                        # immediate second request would be rejected with 429
                        # instead of repairing anything.
                        try:
                            groq_wait = float(
                                _env("AI_GROQ_REPAIR_WAIT_SECONDS", "60") or "60"
                            )
                        except (TypeError, ValueError):
                            groq_wait = 60.0
                        if groq_wait > 0:
                            print(
                                "AI router: waiting "
                                f"{groq_wait:.0f}s for the Groq free-plan token "
                                "window before the repair attempt."
                            )
                            time.sleep(groq_wait)
                    request_prompt = _repair_prompt(prompt, exc)
                    print(
                        f"AI router: {route.provider}/{route.model} returned "
                        "invalid JSON/schema; retrying this provider once with "
                        "a targeted correction."
                    )
                    continue

                errors.append(
                    f"{route.provider}/{route.model}: invalid response ({exc})"
                )
                _DISABLED_ROUTES.add((task, route.route_id))
                print(
                    f"AI router: {route.provider}/{route.model} returned "
                    f"invalid data after the bounded repair attempt; trying "
                    f"another provider. {exc}"
                )
                break

    if transient_routes and not _transient_retry:
        # Retry only the routes that produced a transient transport/server
        # failure. Authentication, billing, quota, and malformed-output routes
        # are not retried as if they were temporary server outages.
        print(
            f"AI router: all available {task} routes had transient errors; "
            "one bounded retry in 5s."
        )
        time.sleep(5)

        for route in transient_routes:
            _COOLDOWN_UNTIL.pop((task, route.route_id), None)

        return _route(
            task,
            prompt,
            images=images,
            validator=validator,
            _transient_retry=True,
            _prior_errors=errors,
        )

    if not routes:
        message = "No configured AI key for this task."
        if free_tier_only():
            message += (
                " Free-tier mode is on, so paid routes (DeepSeek and paid "
                "OpenRouter models) were deliberately not used."
            )
    elif not errors:
        message = "All configured providers are disabled or cooling down."
    else:
        message = "; ".join(errors)

    if task == "vision":
        raise VisionServiceUnavailable(
            "All visual verification providers are unavailable: " + message
        )

    raise ProviderRoutingError(
        "All text-generation providers are unavailable: " + message
    )


def generate_json(
    prompt: str,
    validator: Callable[[Any], Any] | None = None,
) -> tuple[Any, str, str]:
    """Generate documentary-planning JSON using the text-provider route."""
    return _route("text", prompt, validator=validator)


def generate_fact_json(
    prompt: str,
    validator: Callable[[Any], Any] | None = None,
) -> tuple[Any, str, str]:
    """Run an independent evidence-audit task with its own provider order/cooldown."""
    return _route("fact", prompt, validator=validator)


def verify_images(frames: list[bytes], prompt: str) -> dict[str, Any]:
    """Verify clip frames. A valid REJECT is returned, not routed around."""
    result, _provider, _model = _route(
        "vision",
        prompt,
        images=frames,
    )
    return result
