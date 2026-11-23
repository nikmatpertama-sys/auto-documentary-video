
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


_DISABLED_PROVIDERS: set[str] = set()
_DISABLED_ROUTES: set[str] = set()
_COOLDOWN_UNTIL: dict[str, float] = {}
_TRANSIENT_COOLDOWN_SECONDS = 3600


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _order(name: str, default: str) -> list[str]:
    values = [x.strip().lower() for x in _env(name, default).split(",")]
    return list(dict.fromkeys(x for x in values if x))


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
        order = _order("AI_TEXT_PROVIDER_ORDER", "groq,deepseek,gemini,openrouter")
    else:
        order = _order("AI_VISION_PROVIDER_ORDER", "groq,deepseek,gemini,openrouter")

    routes: list[Route] = []
    for provider in order:
        if provider == "groq":
            key = _env("GROQ_API_KEY")
            model = _env(
                "GROQ_MODEL" if task == "text" else "GROQ_VISION_MODEL",
                "openai/gpt-oss-120b" if task == "text" else "qwen/qwen3.8-27b",
            )
            if key and model:
                routes.append(Route(
                    "groq", model, key,
                    "https://api.groq.com/openai/v1/chat/completions",
                    "openai",
                ))
        elif provider == "deepseek":
            key = _env("DEEPSEEK_API_KEY")
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
                "GEMINI_MODEL" if task == "text" else "GEMINI_VISION_MODEL",
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
                "OPENROUTER_MODEL" if task == "text" else "OPENROUTER_VISION_MODEL",
                "openrouter/free" if task == "text" else "nvidia/nemotron-3.5-super-vl:free",
            )
            if key and model:
                routes.append(Route(
                    "openrouter", model, key,
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


def _mark_failure(route: Route, status: int | None, detail: str) -> None:
    detail = str(detail or "").replace(route.api_key, "[REDACTED]")
    detail = re.sub(r"(?i)Bearer\s+[^ ,;]+", "Bearer [REDACTED]", detail)

    if status == 429:
        _DISABLED_PROVIDERS.add(route.provider)
        print(
            f"AI router: {route.provider} rate/quota limit (HTTP 429); "
            "provider skipped for the rest of this run."
        )
    elif status in (401, 403):
        _DISABLED_PROVIDERS.add(route.provider)
        print(
            f"AI router: {route.provider} authentication/permission error "
            f"(HTTP {status}); provider disabled for this run."
        )
    elif status in (400, 404, 413, 415, 422):
        _DISABLED_ROUTES.add(route.route_id)
        print(
            f"AI router: route {route.route_id} rejected configuration/request "
            f"(HTTP {status}); trying another route. {detail}"
        )
    elif status is not None or detail:
        _COOLDOWN_UNTIL[route.route_id] = (
            time.monotonic() + _TRANSIENT_COOLDOWN_SECONDS
        )
        print(
            f"AI router: {route.route_id} temporarily unavailable; "
            f"switching provider. {detail}"
        )
    else:
        _COOLDOWN_UNTIL[route.route_id] = (
            time.monotonic() + _TRANSIENT_COOLDOWN_SECONDS
        )
        print(
            f"AI router: {route.route_id} connection timeout/network error; "
            "switching provider."
        )


def _usable(route: Route) -> bool:
    if route.provider in _DISABLED_PROVIDERS or route.route_id in _DISABLED_ROUTES:
        return False
    if _COOLDOWN_UNTIL.get(route.route_id, 0) > time.monotonic():
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
        if task == "text"
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
        "temperature": 0.25 if task == "text" else 0.05,
    }

    if task == "text":
        payload["max_tokens"] = 7000

        if route.provider == "deepseek":
            payload["response_format"] = {"type": "json_object"}
            payload["thinking"] = {"type": "disabled"}

        if route.provider == "groq":
            payload.pop("max_tokens", None)
            payload["max_completion_tokens"] = 7000
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
        raise _HTTPFailure(response.status_code, _safe_detail(response))

    try:
        data = response.json()
        content = data["choices"][0]["message"].get("content")

        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
            )

        content = str(content or "").strip()

        if not content:
            raise ValueError("empty message content")

        return content

    except Exception as exc:
        raise ValueError(
            f"Unreadable OpenAI-compatible response ({type(exc).__name__})"
        ) from exc


class _HTTPFailure(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


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
            "temperature": 0.25 if task == "text" else 0.05,
            "maxOutputTokens": 7000 if task == "text" else 600,
        },
    }

    response = requests.post(
        route.endpoint,
        headers={
            "x-goog-api-key": route.api_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(12, 50 if task == "vision" else 120),
    )

    if response.status_code >= 400:
        raise _HTTPFailure(response.status_code, _safe_detail(response))

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


def _route(
    task: str,
    prompt: str,
    *,
    images: list[bytes] | None = None,
    validator: Callable[[Any], Any] | None = None,
    _transient_retry: bool = False,
) -> tuple[Any, str, str]:
    errors: list[str] = []
    transient_routes: list[Route] = []
    routes = _routes(task)

    for route in routes:
        if not _usable(route):
            continue

        print(
            f"AI router: trying {route.provider} / {route.model} "
            f"for {task}..."
        )

        try:
            route_images = images

            if images and route.provider == "groq":
                route_images = [images[len(images) // 2]]
            elif images:
                route_images = [images[0], images[-1]]

            raw = _request(route, prompt, route_images, task)
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
                        and confidence >= float(
                            _env("FOOTAGE_VISION_MIN_CONFIDENCE", "0.72")
                        )
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
            _mark_failure(route, exc.status, exc.detail)

            if exc.status in (408, 500, 502, 503, 504):
                transient_routes.append(route)

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            errors.append(
                f"{route.provider}/{route.model}: {type(exc).__name__}"
            )
            _mark_failure(route, None, "network timeout/connection error")
            transient_routes.append(route)

        except (ValueError, KeyError, TypeError) as exc:
            errors.append(
                f"{route.provider}/{route.model}: invalid response ({exc})"
            )
            _DISABLED_ROUTES.add(route.route_id)
            print(
                f"AI router: {route.provider}/{route.model} returned "
                f"invalid data; trying another provider. {exc}"
            )

        except requests.exceptions.RequestException as exc:
            errors.append(
                f"{route.provider}/{route.model}: {type(exc).__name__}"
            )
            _mark_failure(route, None, "request transport error")
            transient_routes.append(route)

    if transient_routes and not _transient_retry:
        print(
            f"AI router: all available {task} routes had transient errors; "
            "one bounded retry in 5s."
        )
        time.sleep(5)

        for route in transient_routes:
            _COOLDOWN_UNTIL.pop(route.route_id, None)

        return _route(
            task,
            prompt,
            images=images,
            validator=validator,
            _transient_retry=True,
        )

    if not routes:
        message = "No configured AI key for this task."
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
    """Generate JSON using bounded provider failover; returns value/provider/model."""
    return _route("text", prompt, validator=validator)


def verify_images(frames: list[bytes], prompt: str) -> dict[str, Any]:
    """Verify clip frames. A valid REJECT is returned, not routed around."""
    result, _provider, _model = _route(
        "vision",
        prompt,
        images=frames,
    )
    return result
