#!/usr/bin/env python3
"""Diagnose Speechify and prove that text-to-speech really returns audio.

Two questions are answered separately, because they have different causes:

1. Why does ``GET /v1/voices`` return HTTP 403? The catalog request is retried
   with several documented parameter combinations (including the production one
   that adds ``model``), and the API error ``code`` is recorded. HTTP 403 from
   Speechify means "the credential authenticated but is not authorised for this
   resource"; HTTP 401 means the credential itself is rejected. The distinction
   decides whether the key is broken or only the catalog listing is gated.

2. Does ``POST /v1/audio/speech`` really return playable audio? The body is
   built by the same function :mod:`main` uses for production narration, and the
   returned base64 payload is decoded, written to disk and measured (duration,
   peak amplitude, word marks). A 200 with an empty payload is a failure.

No credential value, prefix or hash is ever printed or written. Only names,
HTTP status codes, API error codes and audio measurements appear in the report.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

API_ROOT = "https://api.speechify.ai"
VOICES_URL = f"{API_ROOT}/v1/voices"
SPEECH_URL = f"{API_ROOT}/v1/audio/speech"

# The documented example voice id from the Speechify quickstart. Tried only
# after a pinned SPEECHIFY_VOICE_ID and after any id an authorised catalog call
# actually returns, so a passing result always points at a real catalog voice.
DOCUMENTED_EXAMPLE_VOICE = "geffen_32"

# Fallbacks the pipeline itself can select when the catalog is unreachable.
PIPELINE_FALLBACK_VOICES = ("george", "henry")

PROBE_TEXT = (
    "Every evening, the canopy of the Amazon rainforest releases moisture "
    "into the sky, seeding rain hundreds of kilometres away."
)

MAX_KEYS = 4
MAX_VOICES = 6
TIMEOUT_SECONDS = 60


def _present(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def _numbered(base: str) -> list[str]:
    pattern = re.compile(r"^" + re.escape(base) + r"_(?:\d+)$", re.I)
    found: list[tuple[int, str]] = []
    for env_name in os.environ:
        match = pattern.match(env_name)
        if match and _present(env_name):
            try:
                index = int(env_name.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                continue
            found.append((index, env_name))
    return [name for _, name in sorted(found)]


def _keys() -> list[tuple[str, str]]:
    """Return (env name, value) pairs, in pipeline order, without duplicates."""
    names: list[str] = []
    for base in ("SPVO_API", "SPEECHIFY_API_KEY"):
        names.append(base)
        names.extend(_numbered(base))
    pairs: list[tuple[str, str]] = []
    seen_values: set[str] = set()
    for name in names:
        if not _present(name):
            continue
        value = os.environ[name].strip()
        if value in seen_values:
            continue
        seen_values.add(value)
        pairs.append((name, value))
    return pairs[:MAX_KEYS]


def _error_code(payload: bytes) -> str:
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        if code:
            return code
    if isinstance(error, str):
        return error[:60]
    return ""


def _get(url: str, token: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "speechify-tts-probe/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read() or b"", dict(error.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, str(getattr(error, "reason", error)).encode(), {}


def _post(url: str, token: str, body: dict) -> tuple[int, bytes, dict[str, str]]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "speechify-tts-probe/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read() or b"", dict(error.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, str(getattr(error, "reason", error)).encode(), {}


# --------------------------------------------------------------------------
# Exactly what main.py sends for production narration.
# --------------------------------------------------------------------------
def production_ssml(sentence: str, index: int, total: int) -> str:
    escaped = html.escape(sentence, quote=False)
    if index == 0:
        rate, pitch = "-3%", "-4%"
    elif sentence.rstrip().endswith("?"):
        rate, pitch = "-3%", "-3%"
    elif index == total - 1:
        rate, pitch = "-7%", "-10%"
    else:
        rate, pitch = "-6%", "-8%"
    return f'<speak><prosody rate="{rate}" pitch="{pitch}">{escaped}</prosody></speak>'


def production_body(voice_id: str, text: str = PROBE_TEXT, model: str | None = None) -> dict:
    return {
        "input": production_ssml(text, 0, 1),
        "voice_id": voice_id,
        "audio_format": "wav",
        "language": "en-US",
        "model": (model or os.getenv("SPEECHIFY_MODEL", "simba-3.5")).strip() or "simba-3.5",
    }


CATALOG_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("production", {"type": "shared", "locale": "en-US", "gender": "male", "model": "simba-3.5", "limit": 20}),
    ("documented", {"type": "shared", "locale": "en-US", "gender": "male", "limit": 20}),
    ("no_type_filter", {"locale": "en-US", "gender": "male", "limit": 20}),
    ("bare", {"limit": 1}),
)


def _voice_descriptor(voice: dict) -> str:
    tags = voice.get("tags")
    tags_text = " ".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags or "")
    return " ".join(
        str(value)
        for value in (
            voice.get("display_name", ""),
            voice.get("name", ""),
            voice.get("description", ""),
            tags_text,
        )
    ).lower()


def probe_catalog(key_name: str, token: str) -> dict:
    variants: list[dict] = []
    catalog_voices: list[dict] = []
    query_model = os.getenv("SPEECHIFY_MODEL", "simba-3.5").strip() or "simba-3.5"
    for label, params in CATALOG_VARIANTS:
        params = dict(params)
        if params.get("model") == "simba-3.5":
            params["model"] = query_model
        status, payload, _ = _get(f"{VOICES_URL}?{urllib.parse.urlencode(params)}", token)
        entry = {
            "variant": label,
            "params": sorted(params),
            "http_status": status,
            "error_code": _error_code(payload) if status >= 400 else "",
        }
        if 200 <= status < 300:
            try:
                data = json.loads(payload.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                data = {}
            voices = data.get("voices", data.get("data", [])) if isinstance(data, dict) else data
            if isinstance(voices, dict):
                voices = voices.get("voices", [])
            if isinstance(voices, list):
                entry["voice_count"] = len(voices)
                if not catalog_voices:
                    catalog_voices = [voice for voice in voices if isinstance(voice, dict)]
            entry["has_more"] = bool(data.get("has_more")) if isinstance(data, dict) else False
        variants.append(entry)
    return {"env": key_name, "variants": variants, "catalog_voices": catalog_voices}


def _measure_wav(path: str) -> dict:
    try:
        with wave.open(path, "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            raw = handle.readframes(frames)
    except (wave.Error, EOFError, OSError) as error:
        return {"valid": False, "error": str(error)[:120]}
    duration = frames / float(rate)
    peak = 0
    if width == 2 and raw:
        samples = struct.unpack("<%dh" % (len(raw) // 2), raw[: (len(raw) // 2) * 2])
        peak = max(abs(sample) for sample in samples) if samples else 0
    return {
        "valid": duration > 0.2 and peak > 0,
        "duration_seconds": round(duration, 3),
        "sample_rate": rate,
        "channels": channels,
        "peak_amplitude": peak,
        "silent": peak == 0,
    }


def _saved_mark_count(marks) -> int:
    count = 0

    def walk(item):
        nonlocal count
        if isinstance(item, dict):
            if item.get("start_time") is not None and item.get("end_time") is not None:
                count += 1
            for child in item.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(marks)
    return count


def candidate_voices(catalog_voices: list[dict]) -> list[dict]:
    """Pinned secret first, then real catalog voices, then documented fallbacks."""
    ordered: list[dict] = []
    seen: set[str] = set()

    def add(voice_id: str, source: str) -> None:
        voice_id = (voice_id or "").strip()
        if not voice_id or voice_id in seen:
            return
        seen.add(voice_id)
        ordered.append({"voice_id": voice_id, "source": source})

    if _present("SPEECHIFY_VOICE_ID"):
        add(os.environ["SPEECHIFY_VOICE_ID"], "SPEECHIFY_VOICE_ID secret")

    scored: list[tuple[int, str]] = []
    for voice in catalog_voices:
        voice_id = str(voice.get("id") or voice.get("voice_id") or "").strip()
        if not voice_id:
            continue
        descriptor = _voice_descriptor(voice)
        score = 0
        for term, points in (
            ("calm", 5), ("deep", 5), ("baritone", 6), ("narration", 3), ("documentary", 4),
        ):
            if term in descriptor:
                score += points
        scored.append((score, voice_id))
    scored.sort(reverse=True)
    for _, voice_id in scored[:3]:
        add(voice_id, "voices catalog")

    add(DOCUMENTED_EXAMPLE_VOICE, "documented quickstart example")
    for voice_id in PIPELINE_FALLBACK_VOICES:
        add(voice_id, "pipeline built-in fallback")
    return ordered[:MAX_VOICES]


def probe_speech(key_name: str, token: str, voices: list[dict], sample_dir: str) -> list[dict]:
    results: list[dict] = []
    for candidate in voices:
        body = production_body(candidate["voice_id"])
        started = time.time()
        status, payload, _ = _post(SPEECH_URL, token, body)
        elapsed = round(time.time() - started, 2)
        entry = {
            "env": key_name,
            "voice_id": candidate["voice_id"],
            "voice_source": candidate["source"],
            "model": body["model"],
            "audio_format": body["audio_format"],
            "http_status": status,
            "elapsed_seconds": elapsed,
            "error_code": _error_code(payload) if status >= 400 else "",
        }
        if 200 <= status < 300:
            try:
                data = json.loads(payload.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                data = {}
            audio_data = data.get("audio_data") or data.get("audio")
            if isinstance(audio_data, str) and ",base64," in audio_data:
                audio_data = audio_data.split(",base64,", 1)[1]
            if isinstance(audio_data, str) and audio_data:
                raw = base64.b64decode(audio_data)
                entry["audio_bytes"] = len(raw)
                entry["billable_characters"] = data.get("billable_characters_count")
                entry["word_marks"] = _saved_mark_count(data.get("speech_marks"))
                sample_path = os.path.join(sample_dir, f"speechify_sample_{candidate['voice_id']}.wav")
                with open(sample_path, "wb") as handle:
                    handle.write(raw)
                entry["measurement"] = _measure_wav(sample_path)
                entry["audio_ok"] = bool(entry["measurement"].get("valid"))
                entry["sample_file"] = os.path.basename(sample_path)
            else:
                entry["audio_bytes"] = 0
                entry["audio_ok"] = False
                entry["error_code"] = entry["error_code"] or "empty_audio_payload"
        else:
            entry["audio_ok"] = False
        results.append(entry)
        if entry.get("audio_ok"):
            break
    return results


def run(sample_dir: str = "speechify_samples") -> dict:
    keys = _keys()
    os.makedirs(sample_dir, exist_ok=True)
    report: dict = {
        "speechify_credentials_present": [name for name, _ in keys],
        "voice_id_secret_set": _present("SPEECHIFY_VOICE_ID"),
        "catalog": [],
        "speech": [],
        "warnings": [],
    }
    if not keys:
        report["warnings"].append("no Speechify credential reached this process")
        report["tts_audio_produced"] = False
        return report

    working_voices: list[dict] = []
    for key_name, token in keys[:2]:
        catalog = probe_catalog(key_name, token)
        report["catalog"].append(
            {"env": catalog["env"], "variants": catalog["variants"], "catalog_size": len(catalog["catalog_voices"])}
        )
        if catalog["catalog_voices"] and not working_voices:
            working_voices = catalog["catalog_voices"]

    voices = candidate_voices(working_voices)
    report["candidate_voices"] = [voice["voice_id"] for voice in voices]
    for key_name, token in keys:
        report["speech"].extend(probe_speech(key_name, token, voices, sample_dir))
        if any(item.get("audio_ok") for item in report["speech"]):
            break

    report["tts_audio_produced"] = any(item.get("audio_ok") for item in report["speech"])
    if not report["tts_audio_produced"]:
        codes = sorted({item["error_code"] for item in report["speech"] if item["error_code"]})
        report["warnings"].append(
            "Speechify returned no usable audio; error codes: " + (", ".join(codes) or "none recorded")
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Speechify catalog access and real TTS audio.")
    parser.add_argument("--json-out", default="speechify_tts_report.json")
    parser.add_argument("--sample-dir", default="speechify_samples")
    parser.add_argument(
        "--require-audio",
        action="store_true",
        help="exit 1 when Speechify produced no usable audio",
    )
    args = parser.parse_args(argv)

    report = run(args.sample_dir)

    print("Speechify credential names reaching this process: " + (", ".join(report["speechify_credentials_present"]) or "none"))
    print("SPEECHIFY_VOICE_ID secret: " + ("SET" if report["voice_id_secret_set"] else "MISSING"))
    print("Voice catalog requests:")
    for item in report["catalog"]:
        for variant in item["variants"]:
            detail = f" voices={variant['voice_count']}" if "voice_count" in variant else ""
            code = f" code={variant['error_code']}" if variant["error_code"] else ""
            print(f"  {item['env']:<16} {variant['variant']:<14} HTTP {variant['http_status']}{code}{detail}")
    print("Production text-to-speech requests:")
    for item in report["speech"]:
        measurement = item.get("measurement") or {}
        detail = ""
        if measurement.get("valid") is not None:
            detail = (
                f" bytes={item.get('audio_bytes', 0)}"
                f" duration={measurement.get('duration_seconds')}s"
                f" peak={measurement.get('peak_amplitude')}"
                f" marks={item.get('word_marks', 0)}"
            )
        code = f" code={item['error_code']}" if item["error_code"] else ""
        print(
            f"  {item['env']:<16} voice={item['voice_id']:<14} HTTP {item['http_status']}{code}{detail}"
        )
    for warning in report["warnings"]:
        print(f"WARN: {warning}", file=sys.stderr)

    with open(args.json_out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(f"Report written to {args.json_out} (no credential values).")

    if args.require_audio and not report["tts_audio_produced"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
