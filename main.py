
import os
import re
import json
import time
import asyncio
import hashlib
import base64
import tempfile
import shutil
import subprocess
import textwrap
import sys
import traceback
import html
import threading
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs, urljoin
from html.parser import HTMLParser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import edge_tts

import footage_license
import footage_queries
import qc as qc_module
import reference_style
import research as research_module
from quota_router import QuotaAwareRouter
from visual_retry import visual_retry_policy, redact_provider_error

# ============================================================
# CONFIG
# ============================================================
OUT = Path("output")
FOOT = OUT / "footage"
CACHE = Path(".api-cache")
QUOTA_ROUTER = QuotaAwareRouter(probe_ttl_seconds=60, timeout=10)

for folder in (OUT, FOOT, CACHE):
    folder.mkdir(parents=True, exist_ok=True)

def _collect_env_values(*base_names):
    """Read one or more numbered API-key secrets without exposing their values."""
    values = []
    seen = set()
    for base in base_names:
        candidates = []
        if os.getenv(base, "").strip():
            candidates.append((0, base))
        pattern = re.compile(r"^" + re.escape(base) + r"(?:_?(\d+))$", re.I)
        numbered = []
        for env_name, env_value in os.environ.items():
            match = pattern.match(env_name)
            if match and str(env_value or "").strip():
                numbered.append((int(match.group(1)), env_name))
        candidates.extend(sorted(numbered, key=lambda item: (item[0], item[1])))
        for _, env_name in candidates:
            value = os.getenv(env_name, "").strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _collect_cloudflare_credentials():
    """Pair every configured Cloudflare token with its matching account ID."""
    credentials = []
    seen = set()
    suffixes = [""]
    for number in range(1, 21):
        suffixes.extend((str(number), f"_{number}"))
    pairs = []
    for suffix in suffixes:
        pairs.append((f"CLOUDFLARE_API_TOKEN{suffix}", f"CLOUDFLARE_ACCOUNT_ID{suffix}"))
    # Explicit aliases supplied for this project.
    for number in range(1, 21):
        pairs.append((f"CF_API_TK{number}", f"CF_ID{number}"))
    # Additional common aliases, while retaining explicit user-provided names.
    pairs.extend([
        ("CF_API_TOKEN", "CF_ACCOUNT_ID"),
        ("CLOUDFLARE_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
    ])
    for token_name, account_name in pairs:
        token = os.getenv(token_name, "").strip()
        account_id = os.getenv(account_name, "").strip()
        if not token or not account_id:
            continue
        fingerprint = hashlib.sha256((token + ":" + account_id).encode()).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        credentials.append({"token": token, "account_id": account_id, "label": token_name})
    return credentials


def _quota_credential_id(provider, secret):
    """Opaque credential identifier; never logs or persists the secret itself."""
    return f"{str(provider).lower()}::{QuotaAwareRouter.fingerprint(secret)}"


def _quota_scope(provider, api_key=None, account_id=None):
    """Return the real or conservative shared quota pool for a provider."""
    name = str(provider).lower()
    if name == "groq":
        # Groq limits are organization-scoped. Optional numbered group IDs let
        # operators separate keys only when they know the keys belong to distinct orgs.
        key_index = next((i for i, value in enumerate(GROQ_KEYS, 1) if value == api_key), None)
        numbered = os.getenv(f"GROQ_QUOTA_GROUP_{key_index}", "").strip() if key_index else ""
        group = numbered or os.getenv("GROQ_QUOTA_GROUP", "shared-organization").strip()
        return f"Groq::{QuotaAwareRouter.fingerprint(group)}"
    if name == "openrouter":
        # Finite spending caps belong to each OpenRouter key.
        return f"OpenRouterKey::{QuotaAwareRouter.fingerprint(api_key)}"
    if name == "gemini":
        # Gemini quotas are project-scoped, not key-scoped. Numbered project IDs
        # are optional for keys deliberately created in different Google projects.
        key_index = next((i for i, value in enumerate(GEMINI_KEYS, 1) if value == api_key), None)
        numbered = os.getenv(f"GEMINI_PROJECT_ID_{key_index}", "").strip() if key_index else ""
        project = numbered or os.getenv("GEMINI_PROJECT_ID", "").strip() or "unknown-shared-project"
        return f"Gemini::{QuotaAwareRouter.fingerprint(project)}"
    if name == "cloudflare":
        # Workers AI usage/rate limits are tied to the Cloudflare account.
        return f"Cloudflare::{QuotaAwareRouter.fingerprint(account_id or 'unknown-account')}"
    if name == "speechify":
        # Speechify request-rate/concurrency budgets are per account.
        group = os.getenv("SPEECHIFY_QUOTA_GROUP", "shared-account").strip()
        return f"Speechify::{QuotaAwareRouter.fingerprint(group)}"
    return f"{name}::shared"


def _openrouter_account_scope(api_key):
    """Group OpenRouter keys that share one account-level free-model budget."""
    key_index = next((i for i, value in enumerate(OPENROUTER_KEYS, 1) if value == api_key), None)
    numbered = os.getenv(f"OPENROUTER_QUOTA_GROUP_{key_index}", "").strip() if key_index else ""
    group = numbered or os.getenv("OPENROUTER_QUOTA_GROUP", "shared-account").strip()
    return f"OpenRouterAccount::{QuotaAwareRouter.fingerprint(group)}"


def _openrouter_model_scope(model):
    return f"OpenRouterModel::{QuotaAwareRouter.fingerprint(model)}" if model else ""


def _is_openrouter_free_model(model):
    value = str(model or "").strip().lower()
    return value == "openrouter/free" or value.endswith(":free")


def _observe_provider_response(provider, response, api_key=None, account_id=None, model=None):
    scope = _quota_scope(provider, api_key=api_key, account_id=account_id)
    credential_id = _quota_credential_id(provider, api_key or account_id or provider)
    shared_scope = _openrouter_account_scope(api_key) if str(provider).lower() == "openrouter" else None
    model_scope = _openrouter_model_scope(model) if str(provider).lower() == "openrouter" else None
    QUOTA_ROUTER.observe_response(
        provider, scope, response, credential_id,
        shared_scope=shared_scope, model_scope=model_scope,
    )
    return scope, credential_id


def _quota_route(provider, name, api_key, callback, account_id=None, route_id=None, model=None):
    scope = _quota_scope(provider, api_key=api_key, account_id=account_id)
    credential_id = _quota_credential_id(provider, api_key or account_id or name)
    route = {
        "id": route_id or f"{str(provider).lower()}::{QuotaAwareRouter.fingerprint(api_key or account_id or name)}",
        "name": name,
        "provider": provider,
        "scope": scope,
        "credential_id": credential_id,
        "run": callback,
    }
    if str(provider).lower() == "openrouter":
        route["probe_key"] = api_key
        route["shared_scope"] = _openrouter_account_scope(api_key)
        route["model"] = str(model or OPENROUTER_MODEL)
        route["model_scope"] = _openrouter_model_scope(route["model"])
        route["uses_free_model"] = _is_openrouter_free_model(route["model"])
    return route


GROQ_KEYS = _collect_env_values("GROQ_API_KEY")
OPENROUTER_KEYS = _collect_env_values("OPENROUTER_API_KEY")
GEMINI_KEYS = _collect_env_values("GEMINI_API_KEY", "GOOGLE_API_KEY")
SPEECHIFY_KEYS = _collect_env_values("SPVO_API", "SPEECHIFY_API_KEY")
CLOUDFLARE_CREDENTIALS = _collect_cloudflare_credentials()

# Backwards-compatible first-key aliases used by older helper code.
GROQ_KEY = GROQ_KEYS[0] if GROQ_KEYS else ""
OPENROUTER_KEY = OPENROUTER_KEYS[0] if OPENROUTER_KEYS else ""
GEMINI_KEY = GEMINI_KEYS[0] if GEMINI_KEYS else ""
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
PEXELS_KEY = os.getenv("PEXELS_API_KEY", "").strip()
COVERR_API_KEY = os.getenv("COVERR_API_KEY", "").strip()
ENABLE_COVERR_SOURCE = bool(COVERR_API_KEY)
TOPIC = os.getenv("INPUT_TOPIC", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "openai/gpt-oss-120b"
).strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "openrouter/free"
).strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
CLOUDFLARE_CHAT_MODEL = os.getenv(
    "CLOUDFLARE_CHAT_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
).strip()
CLOUDFLARE_VISION_MODEL = os.getenv(
    "CLOUDFLARE_VISION_MODEL", "@cf/meta/llama-3.2-11b-vision-instruct"
).strip()
SPEECHIFY_MODEL = os.getenv("SPEECHIFY_MODEL", "simba-3.5").strip() or "simba-3.5"
SPEECHIFY_VOICE_ID = os.getenv("SPEECHIFY_VOICE_ID", "").strip()
# A workspace key can be authorised for speech synthesis while GET /v1/voices
# answers 403 insufficient_scope. Speechify documents this shared voice id, and
# it was verified against the live speech endpoint (HTTP 200, wav audio), so it
# is the built-in voice when the catalog cannot be read and no voice is pinned.
SPEECHIFY_DOCUMENTED_VOICE_ID = os.getenv(
    "SPEECHIFY_FALLBACK_VOICE_ID", "geffen_32"
).strip() or "geffen_32"
# Filled at generation time so the manifest records the voice actually used.
SPEECHIFY_VOICE_USED = ""

# MM6: the reference format is an Indonesian vertical documentary. The script
# language also drives the TTS voice, caption casing and vocabulary checks.
LANGUAGE = reference_style.language_profile(os.getenv("SCRIPT_LANGUAGE", ""))
SCRIPT_LANGUAGE = LANGUAGE["code"]
CAPTION_STYLE = reference_style.caption_style(SCRIPT_LANGUAGE)
VOICE = os.getenv("TTS_VOICE", "").strip() or LANGUAGE["edge_voice"]  # Edge TTS
BASE_RATE = os.getenv("TTS_RATE", "-4%").strip()
BASE_PITCH = os.getenv("TTS_PITCH", "-3Hz").strip()
# Set by the duration-recovery loop when the measured VO is shorter than 70s.
VO_RATE_OVERRIDE = None
MIN_VIDEO_DURATION = 70.0
MAX_VIDEO_DURATION = None  # Intentionally unlimited; final duration follows the complete VO.

# MM6 presentation: a locally synthesised royalty-free music bed and subtle
# transition sound design, both generated with FFmpeg (no third-party audio).
MUSIC_BED_ENABLED = os.getenv("MUSIC_BED", "true").strip().lower() not in (
    "0", "false", "no", "off"
)
SFX_ENABLED = os.getenv("SFX_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off"
)
try:
    MUSIC_GAIN_DB = float(os.getenv("MUSIC_GAIN_DB", "-23"))
except ValueError:
    MUSIC_GAIN_DB = -23.0
try:
    SFX_GAIN_DB = float(os.getenv("SFX_GAIN_DB", "-17"))
except ValueError:
    SFX_GAIN_DB = -17.0

W, H, FPS = 1080, 1920, 30
TARGET_SOURCE_ASPECT = 9 / 16
SOURCE_ASPECT_TOLERANCE = 0.003  # Native 9:16 is preferred; verified landscape reframing is allowed.
MIN_CLIP_SHORT_SIDE = 640
MIN_REFRAME_SOURCE_HEIGHT = 640
MAX_REFRAME_SOURCE_ASPECT = 2.40
MIN_REFRAME_SOURCE_ASPECT = 1.18
SMART_CROP_FRAME_COUNT = 6
SMART_CROP_MIN_BOX_CONFIDENCE = 0.55
SMART_CROP_BOX_MARGIN_FRACTION = 0.045
SMART_CROP_MAX_PAN_RATE_PER_SECOND = 0.55
CAPTION_MAX_WORDS = CAPTION_STYLE["max_words"]
CAPTION_MAX_CHARS = CAPTION_STYLE["max_chars"]
X264_PRESET = os.getenv("X264_PRESET", "veryfast").strip() or "veryfast"
X264_CRF = os.getenv("X264_CRF", "19").strip() or "19"

OPENING_LEAD = 0.75
SENTENCE_GAP = 0.30
HOOK_GAP = 0.70
# Only a tiny tail so the last phoneme is not clipped. The format requires the
# video to end when the narration ends, so there is deliberately no long hold.
ENDING_HOLD = 0.30

MIN_CLIP_WIDTH = 640
# Ordinary candidates keep the original 180 MB budget. Highly relevant footage
# may use a larger bounded allowance when disk space is available.
MAX_CLIP_BYTES = 180 * 1024 * 1024
MAX_ADAPTIVE_CLIP_BYTES = 650 * 1024 * 1024
MAX_EXCEPTIONAL_CLIP_BYTES = 900 * 1024 * 1024
DOWNLOAD_DISK_RESERVE_BYTES = 1536 * 1024 * 1024
DOWNLOAD_MAX_SECONDS = 15 * 60

# Visual verification is deliberately fail-closed by default. Set this to
# false only if you explicitly accept metadata-only footage selection.
VISION_REQUIRED = os.getenv("FOOTAGE_VISION_REQUIRED", "true").strip().lower() not in (
    "0", "false", "no", "off"
)

# Search more of the metadata-ranked pool before declaring a scene impossible.
VISION_CANDIDATE_LIMIT = max(
    1, min(50, int(os.getenv("FOOTAGE_VISION_CANDIDATE_LIMIT", "36")))
)

# MM4: official Coverr API search for free, authentic native 9:16 vertical stock footage.
# MM5: adaptive large-footage downloads and AI-verified dynamic 9:16 reframing.
# MM3: persistent provider cooldowns, independent source caches, request governor and NASA manifest cache.
# MM2: defer orientation rejection until ffprobe can read display-rotation metadata.
# MM1: intent-aware footage ranking, rejection-guided search retry, and source resilience.
# Base release: project1-10.21.
# Version 10.21: persist generated Markdown cleanup to GitHub branches.
# Version 10.19: tiered metadata discovery, broader candidate pool, balanced stock-source priority, and footage diagnostics.
# Version 10.18: accept common stock-library forest/tree metadata for rainforest close-ups; keep visual verification mandatory.
# Version 10.15: quota-aware provider routing using live quota telemetry and shared account/project scopes.
# Version 10.14: provider key rotation, Cloudflare fallback, Speechify VO, 70s+ duration, 120-200 words.
# Version 10.13: preserve hook/subtitle readability and reject non-native 9:16 footage.
# Version 10.12: recover from Wikimedia download throttling and search sparse tiers better.
# Open archives are preferred only when candidates are similarly relevant;
# a much stronger semantic match can outrank source preference. Stock libraries
# remain opt-in because their catalogs repeatedly produced poor scene matches.
ENABLE_PEXELS_FALLBACK = os.getenv("ENABLE_PEXELS_FALLBACK", "false").strip().lower() in ("1", "true", "yes", "on")
ENABLE_PIXABAY_FALLBACK = os.getenv("ENABLE_PIXABAY_FALLBACK", "false").strip().lower() in ("1", "true", "yes", "on")
FOOTAGE_SOURCE_PRIORITY = (
    "Coverr",
    "Internet Archive",
    "Wikimedia Commons",
    "NASA",
    "Web Search",
    "Google Search",
    "Pexels",
    "Pixabay",
)
FOOTAGE_SOURCE_CANDIDATE_LIMITS = {
    "Coverr": 12,
    "Internet Archive": 20,
    "Wikimedia Commons": 16,
    "NASA": 12,
    "Web Search": 10,
    "Google Search": 12,
    "Pexels": 4,
    "Pixabay": 4,
}

try:
    VISION_MIN_CONFIDENCE = float(
        os.getenv("FOOTAGE_VISION_MIN_CONFIDENCE", "0.72")
    )
except ValueError:
    VISION_MIN_CONFIDENCE = 0.72

VISION_MIN_CONFIDENCE = max(0.0, min(1.0, VISION_MIN_CONFIDENCE))

VISION_MODEL = (
    os.getenv("GEMINI_VISION_MODEL", MODEL).strip() or MODEL
)

GROQ_VISION_MODEL = (
    os.getenv(
        "GROQ_VISION_MODEL",
        "qwen/qwen3.8-27b"
    ).strip()
    or "qwen/qwen3.8-27b"
)

OPENROUTER_VISION_MODEL = (
    os.getenv(
        "OPENROUTER_VISION_MODEL",
        "nvidia/nemotron-3.5-super-vl:free"
    ).strip()
    or "nvidia/nemotron-3.5-super-vl:free"
)

OPENROUTER_VISION_FALLBACK_MODEL = (
    os.getenv(
        "OPENROUTER_VISION_FALLBACK_MODEL",
        "inclusionai/ling-3.2-flash:free"
    ).strip()
    or "inclusionai/ling-3.2-flash:free"
)

# Avoid silently using the dynamic free router for vision: it may select a
# reasoning model that spends its output budget before returning the verdict.
if OPENROUTER_VISION_MODEL.lower() == "openrouter/free":
    OPENROUTER_VISION_MODEL = "nvidia/nemotron-3.5-super-vl:free"

# Hard model/config errors disable only one route. Quota and transient errors
# use cooldowns, so a provider may recover later during the same workflow run.
VISION_DISABLED_PROVIDERS = set()
VISION_PROVIDER_COOLDOWN_UNTIL = {}
FOOTAGE_SOURCE_DISABLED = set()  # retained for compatibility with cached runs
FOOTAGE_SOURCE_COOLDOWN_UNTIL = {}
# MM3: polite, bounded request pacing for every footage provider. These limits
# are deliberately generous ceilings, not targets; source-specific cache hits
# should make most repeat queries avoid the network entirely.
FOOTAGE_SOURCE_MIN_INTERVAL = {
    "Coverr": 1.5,
    "Internet Archive": 1.0,
    "Wikimedia Commons": 2.0,
    "NASA": 1.0,
    "Web Search": 2.5,
    "Google Search": 5.0,
    "Pexels": 1.0,
    "Pixabay": 1.0,
}
FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN = {
    # Coverr Demo keys have a documented 50 requests/hour ceiling. The pipeline
    # makes at most two targeted Coverr searches per scene, capped conservatively.
    "Coverr": 18,
    "Internet Archive": 240,
    # Commons is the workhorse for nature footage and allows paced bulk search.
    # 48 was exhausted before the last scenes of a run, which left them with no
    # discovery at all ("no candidate passed the metadata filter").
    "Wikimedia Commons": 200,
    "NASA": 240,
    "Web Search": 80,
    "Google Search": 36,
    "Pexels": 48,
    "Pixabay": 48,
}
# MM7: the run budget scales with the script's actual scene count instead of a
# fixed ceiling that ran out before the last scenes of a long video. These are
# still polite ceilings, not targets; cached queries cost nothing.
FOOTAGE_SOURCE_PER_SCENE_BUDGET = {
    "Coverr": 2,
    "Internet Archive": 14,
    "Wikimedia Commons": 16,
    "NASA": 8,
    "Web Search": 8,
    "Google Search": 3,
}
FOOTAGE_SOURCE_REQUEST_CEILING = {
    "Coverr": 30,
    "Internet Archive": 720,
    "Wikimedia Commons": 900,
    "NASA": 480,
    "Web Search": 260,
    "Google Search": 90,
}


def configure_footage_budget(scene_count):
    """Grow each provider's per-run request budget with the real scene count."""
    scene_count = max(1, int(scene_count or 1))
    budgets = {}
    for source, per_scene in FOOTAGE_SOURCE_PER_SCENE_BUDGET.items():
        configured = int(FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN.get(source, 0) or 0)
        dynamic = 40 + per_scene * scene_count
        ceiling = int(FOOTAGE_SOURCE_REQUEST_CEILING.get(source, dynamic))
        budget = min(ceiling, max(configured, dynamic))
        FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN[source] = budget
        budgets[source] = budget
    print(
        f"Footage request budget for {scene_count} scene(s): "
        + ", ".join(f"{source}={budget}" for source, budget in budgets.items())
        + ". Cached searches do not consume it."
    )
    return budgets


def footage_source_budget_remaining(source):
    maximum = int(FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN.get(source, 0) or 0)
    used = int(FOOTAGE_SOURCE_REQUEST_COUNTS.get(source, 0) or 0)
    return max(0, maximum - used)


FOOTAGE_SOURCE_REQUEST_COUNTS = {}
FOOTAGE_SOURCE_LAST_REQUEST_AT = {}
FOOTAGE_SOURCE_REQUEST_LOCK = threading.Lock()
FOOTAGE_SOURCE_CACHE_TTL = {
    "Coverr": 6 * 3600,
    "Internet Archive": 6 * 3600,
    "Wikimedia Commons": 6 * 3600,
    "NASA": 12 * 3600,
    "Web Search": 3 * 3600,
    "Google Search": 3 * 3600,
    "Pexels": 6 * 3600,
    "Pixabay": 6 * 3600,
}
FOOTAGE_SOURCE_STALE_CACHE_TTL = 7 * 86400
FOOTAGE_SOURCE_CACHE_SCHEMA = 3
FOOTAGE_SOURCE_STATE_SCHEMA = 1
FOOTAGE_SOURCE_STATE_FILE = "footage_source_state.json"
ROOT_FOOTAGE_SOURCE_STATE_FILE = Path(
    os.getenv("FOOTAGE_SOURCE_STATE_FILE", FOOTAGE_SOURCE_STATE_FILE)
)
WIKIMEDIA_CONTACT = os.getenv("FOOTAGE_CONTACT_EMAIL", "").strip()
WIKIMEDIA_USER_AGENT = (
    "AutoDocumentaryVideo/3.0 "
    "(+https://github.com/nikmatpertama-sys/auto-documentary-video"
    + (f"; mailto:{WIKIMEDIA_CONTACT}" if WIKIMEDIA_CONTACT else "")
    + ")"
)
# Do not immediately retry Commons 429s; the caller cools the source and continues elsewhere.
MAX_ADAPTIVE_FOOTAGE_QUERIES = 4
MAX_ADAPTIVE_FOOTAGE_CANDIDATES = 12

PIPELINE_STATE = {
    "status": "not_started",
    "current_stage": "startup",
    "stages": [],
}


class VisionProviderError(RuntimeError):
    def __init__(self, provider, status_code, detail):
        self.provider = provider
        self.status_code = status_code
        super().__init__(
            f"{provider} visual verification HTTP {status_code}: {detail}"
        )


class VisionVerificationError(RuntimeError):
    pass


class VisionResponseError(RuntimeError):
    """A provider answered, but its response could not be used safely."""

    pass


def response_retry_after_seconds(response, default_seconds=120):
    """Read Retry-After seconds or HTTP-date; retain long server reset windows."""
    raw = (getattr(response, "headers", {}) or {}).get("Retry-After", "")
    delay = None
    if raw:
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(raw))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = None
    if delay is None:
        delay = float(default_seconds)
    # Keep a defensive one-week ceiling for malformed headers while honoring
    # normal provider reset windows, including daily quota resets.
    return max(15.0, min(7 * 86400.0, float(delay)))


def _source_state_payload():
    """Merge the runtime cache and tracked state file without trusting their contents."""
    paths = [CACHE / FOOTAGE_SOURCE_STATE_FILE, ROOT_FOOTAGE_SOURCE_STATE_FILE]
    merged = {"schema_version": FOOTAGE_SOURCE_STATE_SCHEMA, "updated_at": 0.0, "sources": {}}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != FOOTAGE_SOURCE_STATE_SCHEMA:
            continue
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            continue
        for source, details in sources.items():
            if source not in FOOTAGE_SOURCE_PRIORITY or not isinstance(details, dict):
                continue
            try:
                until = float(details.get("cooldown_until", 0.0))
                updated = float(details.get("updated_at", 0.0))
            except (TypeError, ValueError):
                continue
            if until <= 0:
                continue
            previous = merged["sources"].get(source, {})
            if updated >= float(previous.get("updated_at", 0.0)):
                merged["sources"][source] = {
                    "cooldown_until": until,
                    "updated_at": updated,
                    "last_status": int(details.get("last_status", 0) or 0),
                    "reason": str(details.get("reason", ""))[:100],
                }
            merged["updated_at"] = max(merged["updated_at"], updated)
    return merged


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_source_state(payload):
    """Persist only public provider cooldown metadata; never keys or user data."""
    safe_sources = {}
    for source, details in (payload.get("sources") or {}).items():
        if source not in FOOTAGE_SOURCE_PRIORITY or not isinstance(details, dict):
            continue
        try:
            until = float(details.get("cooldown_until", 0.0))
            updated = float(details.get("updated_at", time.time()))
            last_status = int(details.get("last_status", 0) or 0)
        except (TypeError, ValueError):
            continue
        if until <= 0:
            continue
        safe_sources[source] = {
            "cooldown_until": until,
            "updated_at": updated,
            "last_status": last_status,
            "reason": str(details.get("reason", ""))[:100],
        }
    safe_payload = {
        "schema_version": FOOTAGE_SOURCE_STATE_SCHEMA,
        "updated_at": time.time(),
        "sources": safe_sources,
    }
    # Cache restore gives fast reuse; the small root file survives failed
    # workflow runs because persist_generated_markdown.py commits it in an
    # always() step. Either copy can be missing without breaking the pipeline.
    for path in (CACHE / FOOTAGE_SOURCE_STATE_FILE, ROOT_FOOTAGE_SOURCE_STATE_FILE):
        try:
            _atomic_write_json(path, safe_payload)
        except OSError as exc:
            print(f"Could not persist footage source state to {path}: {exc}")
    return safe_payload


def _restore_footage_source_cooldowns():
    """Hydrate monotonic in-run timers from epoch timestamps saved by older runs."""
    now_epoch = time.time()
    now_monotonic = time.monotonic()
    state = _source_state_payload()
    active = {}
    for source, details in state.get("sources", {}).items():
        remaining = float(details.get("cooldown_until", 0.0)) - now_epoch
        if remaining > 0:
            active[source] = details
            FOOTAGE_SOURCE_COOLDOWN_UNTIL[source] = max(
                FOOTAGE_SOURCE_COOLDOWN_UNTIL.get(source, 0.0),
                now_monotonic + remaining,
            )
        else:
            FOOTAGE_SOURCE_COOLDOWN_UNTIL.pop(source, None)
    root_state = {}
    try:
        root_payload = json.loads(ROOT_FOOTAGE_SOURCE_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(root_payload, dict) and root_payload.get("schema_version") == FOOTAGE_SOURCE_STATE_SCHEMA:
            root_state = root_payload.get("sources", {}) if isinstance(root_payload.get("sources"), dict) else {}
    except (OSError, ValueError, TypeError):
        root_state = {}
    if active != state.get("sources", {}) or active != root_state:
        _write_source_state({"sources": active})


def footage_source_cooldown_remaining(source):
    """Return cooldown left, reloading epoch state to survive a new runner process."""
    if not source:
        return 0.0
    now_mono = time.monotonic()
    remaining = max(0.0, FOOTAGE_SOURCE_COOLDOWN_UNTIL.get(source, 0.0) - now_mono)
    state = _source_state_payload()
    details = state.get("sources", {}).get(source)
    if details:
        remaining = max(remaining, float(details.get("cooldown_until", 0.0)) - time.time())
        if remaining > 0:
            FOOTAGE_SOURCE_COOLDOWN_UNTIL[source] = now_mono + remaining
    if remaining <= 0:
        FOOTAGE_SOURCE_COOLDOWN_UNTIL.pop(source, None)
    return max(0.0, remaining)


def _persist_source_cooldown(source, delay, status, reason):
    state = _source_state_payload()
    now = time.time()
    current = state.setdefault("sources", {}).get(source, {})
    until = max(float(current.get("cooldown_until", 0.0)), now + float(delay))
    state["sources"][source] = {
        "cooldown_until": until,
        "updated_at": now,
        "last_status": int(status or 0),
        "reason": str(reason or "")[:100],
    }
    _write_source_state(state)
    return until


def mark_footage_source_rate_limited(source, response=None):
    """Persist a provider's 429 cooldown across jobs and across failed runs."""
    if not source:
        return 0.0
    default = 300.0 if source == "Wikimedia Commons" else 90.0
    delay = response_retry_after_seconds(response, default)
    until_epoch = _persist_source_cooldown(source, delay, 429, "http_429")
    remaining = max(0.0, until_epoch - time.time())
    FOOTAGE_SOURCE_COOLDOWN_UNTIL[source] = time.monotonic() + remaining
    print(
        f"{source} rate-limited; persisted cooldown for "
        f"{remaining:.0f}s (Retry-After respected when supplied)."
    )
    return remaining


def mark_footage_source_temporarily_unavailable(source, seconds=45.0):
    """Persist a short backoff after network failure or provider 5xx."""
    if not source:
        return 0.0
    delay = max(30.0, min(900.0, float(seconds)))
    until_epoch = _persist_source_cooldown(source, delay, 0, "temporary_unavailable")
    remaining = max(0.0, until_epoch - time.time())
    FOOTAGE_SOURCE_COOLDOWN_UNTIL[source] = time.monotonic() + remaining
    print(
        f"{source} temporarily unavailable; persisted cooldown for "
        f"{remaining:.0f}s."
    )
    return remaining


class FootageSourceBudgetExceeded(RuntimeError):
    """Local per-provider request ceiling reached; no remote request was made."""


class FootageSourceCooldownActive(RuntimeError):
    """A persisted source cooldown prevented a request before it reached the network."""


def _footage_source_get(source, url, **kwargs):
    """Rate-governed GET for footage discovery, manifests, metadata and downloads."""
    remaining = footage_source_cooldown_remaining(source)
    if remaining > 0:
        raise FootageSourceCooldownActive(
            f"{source} is in cooldown for about {remaining:.0f}s; request skipped."
        )
    minimum_interval = float(FOOTAGE_SOURCE_MIN_INTERVAL.get(source, 1.0))
    maximum_requests = int(FOOTAGE_SOURCE_MAX_REQUESTS_PER_RUN.get(source, 120))
    with FOOTAGE_SOURCE_REQUEST_LOCK:
        count = int(FOOTAGE_SOURCE_REQUEST_COUNTS.get(source, 0))
        if count >= maximum_requests:
            raise FootageSourceBudgetExceeded(
                f"{source} reached its local request ceiling ({maximum_requests}) for this run."
            )
        elapsed = time.monotonic() - FOOTAGE_SOURCE_LAST_REQUEST_AT.get(source, 0.0)
        wait = minimum_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        FOOTAGE_SOURCE_REQUEST_COUNTS[source] = count + 1
        FOOTAGE_SOURCE_LAST_REQUEST_AT[source] = time.monotonic()

    try:
        response = requests.get(url, **kwargs)
    except requests.RequestException:
        mark_footage_source_temporarily_unavailable(source, 60.0)
        raise
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 429:
        mark_footage_source_rate_limited(source, response)
    elif status in (408, 425):
        mark_footage_source_temporarily_unavailable(source, 45.0)
    elif 500 <= status <= 599:
        mark_footage_source_temporarily_unavailable(source, 90.0)
    return response


def _footage_source_cache_path(source, query):
    normalized_query = clean_query(query).lower()
    key = hashlib.sha256(
        (f"mm3-source-cache-v{FOOTAGE_SOURCE_CACHE_SCHEMA}|{source}|{normalized_query}").encode("utf-8")
    ).hexdigest()
    return CACHE / f"source-{key}.json"


def _read_footage_source_cache(source, query, allow_stale=False):
    path = _footage_source_cache_path(source, query)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != FOOTAGE_SOURCE_CACHE_SCHEMA
            or payload.get("source") != source
            or payload.get("query") != clean_query(query).lower()
            or not isinstance(payload.get("results"), list)
        ):
            return None
        age = max(0.0, time.time() - float(payload.get("cached_at", 0.0)))
    except (OSError, ValueError, TypeError):
        return None
    ttl = int(FOOTAGE_SOURCE_CACHE_TTL.get(source, 21600))
    if age <= ttl:
        return {"results": payload["results"], "age": age, "stale": False}
    if allow_stale and age <= FOOTAGE_SOURCE_STALE_CACHE_TTL:
        return {"results": payload["results"], "age": age, "stale": True}
    return None


def _write_footage_source_cache(source, query, results):
    payload = {
        "schema_version": FOOTAGE_SOURCE_CACHE_SCHEMA,
        "source": source,
        "query": clean_query(query).lower(),
        "cached_at": time.time(),
        "results": results if isinstance(results, list) else [],
    }
    try:
        _atomic_write_json(_footage_source_cache_path(source, query), payload)
        return True
    except OSError as exc:
        print(f"Could not write {source} search cache: {exc}")
        return False


def _nasa_manifest_cache_path(nasa_id):
    digest = hashlib.sha256(str(nasa_id).encode("utf-8")).hexdigest()
    return CACHE / f"nasa-manifest-{digest}.json"


def _get_nasa_asset_manifest(nasa_id):
    """Reuse NASA asset manifests; only valid responses are cached for 30 days."""
    cache_path = _nasa_manifest_cache_path(nasa_id)
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 30 * 86400:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except (OSError, ValueError):
            pass
    response = _footage_source_get(
        "NASA",
        f"https://images-api.nasa.gov/asset/{quote(str(nasa_id), safe='')}",
        timeout=35,
        headers={"User-Agent": "AutoDocumentaryVideo/3.0 (+https://github.com/nikmatpertama-sys/auto-documentary-video)"},
    )
    response.raise_for_status()
    assets = response.json().get("collection", {}).get("items", [])
    if not isinstance(assets, list):
        assets = []
    try:
        _atomic_write_json(cache_path, assets)
    except OSError:
        pass
    return assets


# Load saved source cooldowns immediately on process startup. Missing/corrupt
# files are expected (for example after a GitHub cache eviction) and harmless.
_restore_footage_source_cooldowns()

def is_wikimedia_url(url):
    """Recognize both Commons API and upload.wikimedia.org media URLs."""
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == "wikimedia.org" or host.endswith(".wikimedia.org")


def run(cmd):
    """Run a command and surface stderr so failures are diagnosable in Actions."""
    result = subprocess.run(
        [str(x) for x in cmd],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        details = (
            result.stderr
            or result.stdout
            or "No diagnostic output."
        ).strip()

        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{details[-5000:]}"
        )

    if result.stdout:
        print(result.stdout, end="")


def ffprobe_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path)
        ],
        capture_output=True,
        text=True,
        check=True
    )

    return max(0.0, float(result.stdout.strip()))



def ffprobe_video_info(path):
    """Return displayed source dimensions, honoring 90-degree rotation metadata."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        stream = (json.loads(result.stdout or "{}").get("streams") or [])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        raise RuntimeError("Source video has no readable video dimensions.") from exc

    rotation = 0.0
    has_side_data_rotation = False
    for side_data in stream.get("side_data_list") or []:
        try:
            if "rotation" in side_data:
                rotation = float(side_data["rotation"])
                has_side_data_rotation = True
                break
        except (TypeError, ValueError):
            pass
    if not has_side_data_rotation:
        try:
            rotation = float((stream.get("tags") or {}).get("rotate", 0.0))
        except (TypeError, ValueError):
            rotation = 0.0

    # A landscape-coded file with a 90-degree display rotation is portrait
    # when played. FFmpeg's normal autorotation is retained during rendering.
    rotated_90 = int(round(abs(rotation))) % 180 == 90
    if rotated_90:
        width, height = height, width

    sar = str(stream.get("sample_aspect_ratio") or "1:1")
    try:
        sar_num, sar_den = (int(value) for value in sar.split(":", 1))
        if sar_num <= 0 or sar_den <= 0:
            raise ValueError("invalid sample aspect ratio")
        # Pixel aspect is inverted when the image is rotated by 90 degrees.
        pixel_aspect = sar_den / sar_num if rotated_90 else sar_num / sar_den
        display_aspect = (width / max(1, height)) * pixel_aspect
    except (ValueError, ZeroDivisionError):
        display_aspect = width / max(1, height)

    return {
        "width": width,
        "height": height,
        "sample_aspect_ratio": sar,
        "aspect_ratio": round(display_aspect, 6),
        "rotation_degrees": rotation,
    }


def is_native_portrait_916(
    width, height, tolerance=SOURCE_ASPECT_TOLERANCE, aspect_ratio=None
):
    """True only for genuine portrait source dimensions close to 9:16."""
    try:
        width, height = int(width), int(height)
        ratio = float(aspect_ratio) if aspect_ratio is not None else width / height
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    if min(width, height) < MIN_CLIP_SHORT_SIDE or height <= width:
        return False
    return abs(ratio - TARGET_SOURCE_ASPECT) <= tolerance


def candidate_metadata_precheck(hit):
    """Reject only clearly under-sized source metadata before downloading.

    Catalog APIs often report encoded width/height without rotation metadata.
    A portrait phone clip can therefore look landscape-coded in search results.
    Aspect ratio is authoritative only after download, when ffprobe_video_info()
    can apply display rotation and sample-aspect-ratio metadata.
    """
    try:
        width = int(float(hit.get("width") or 0))
        height = int(float(hit.get("height") or 0))
    except (TypeError, ValueError):
        return None

    if width > 0 and height > 0:
        if min(width, height) < MIN_CLIP_SHORT_SIDE:
            return "resolution"
    elif max(width, height) > 0 and max(width, height) < MIN_CLIP_SHORT_SIDE:
        return "resolution"
    return None


def has_native_portrait_metadata_hint(hit):
    """Use catalog dimensions as a ranking hint, never as a final orientation verdict."""
    try:
        width = int(float(hit.get("width") or 0))
        height = int(float(hit.get("height") or 0))
    except (TypeError, ValueError):
        return False
    return bool(width and height and is_native_portrait_916(width, height))


def is_smart_reframe_eligible(width, height, aspect_ratio=None):
    """Allow sufficiently large landscape sources for verified 9:16 reframing only."""
    try:
        width = int(width)
        height = int(height)
        aspect = float(aspect_ratio) if aspect_ratio is not None else width / max(1, height)
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    return bool(
        width > height
        and height >= MIN_REFRAME_SOURCE_HEIGHT
        and MIN_REFRAME_SOURCE_ASPECT <= aspect <= MAX_REFRAME_SOURCE_ASPECT
    )


def candidate_download_limit(score, hit=None):
    """Return a bounded byte budget based on relevance and known file size."""
    try:
        relevance = float(score)
    except (TypeError, ValueError):
        relevance = 0.0
    limit = MAX_CLIP_BYTES
    if relevance >= 0.86:
        limit = MAX_EXCEPTIONAL_CLIP_BYTES
    elif relevance >= 0.72:
        limit = MAX_ADAPTIVE_CLIP_BYTES

    hit = hit if isinstance(hit, dict) else {}
    known_size = 0
    for field in ("file_size", "filesize", "size_bytes", "size"):
        try:
            known_size = int(float(hit.get(field) or 0))
        except (TypeError, ValueError):
            continue
        if known_size > 0:
            break
    if known_size > limit:
        raise RuntimeError(
            f"Candidate file is {known_size / 1024 / 1024:.1f} MB, above its "
            f"relevance-based {limit / 1024 / 1024:.0f} MB download budget."
        )
    return limit


def split_sentences(text):
    return [
        x.strip()
        for x in re.findall(
            r"[^.!?]+[.!?]+|[^.!?]+$",
            text
        )
        if x.strip()
    ]


def normalize_words(text):
    return re.findall(
        r"[a-z0-9']+",
        str(text).lower()
    )


def candidate_search_text(hit):
    """All searchable text a provider supplied for one candidate.

    Catalogs disagree about which field holds the useful words: Commons keeps
    the description and categories in extmetadata, Internet Archive in
    ``subject``, a web result in its page title, and every one of them ships a
    filename that often names the subject (``Ant_trail_on_concrete.webm``).
    Normalising them here means relevance decisions behave the same across
    providers and incomplete metadata is not mistaken for irrelevance.
    """
    url_path = ""
    try:
        url_path = unquote(urlparse(str(hit.get("url") or "")).path).replace("_", " ")
    except Exception:
        url_path = ""
    return " ".join([
        str(hit.get("title") or ""),
        str(hit.get("tags") or ""),
        str(hit.get("metadata") or ""),
        str(hit.get("page_url") or ""),
        url_path,
    ])


def candidate_partial_subject_match(hit, query):
    """True when metadata names the scene's subject, even if details are missing.

    Such a candidate is not accepted; it merely earns an AI visual review, which
    is the only authority on whether the footage really matches the scene.
    """
    text_tokens = set(normalize_words(candidate_search_text(hit)))
    if not text_tokens:
        return False
    _, groups = query_concepts(str(query or ""))
    subject_names = {
        "rainforest", "forest", "canopy", "aerial", "river", "waterfall",
        "wildlife", "insect", "orchid", "jaguar", "bird", "rain", "ocean",
        "desert", "forest_floor", "mountain", "research_activity",
        "field_instrument",
    }
    subject_aliases = []
    for name, aliases in groups.items():
        if name in subject_names:
            subject_aliases.append(aliases)
    canonical = footage_queries.canonical_subject(
        footage_queries.content_terms(str(query or ""))
    )
    if canonical:
        subject_aliases.append(footage_queries.SYNONYM_GROUPS.get(canonical, set()))
    return any(text_tokens & set(aliases) for aliases in subject_aliases)


# ============================================================
# AI DOCUMENTARY PLANNER
# ============================================================

def documentary_prompt(topic, research_brief=None):
    """Load the scriptwriter master prompt and bind topic, language, research."""
    prompt_path = Path(__file__).with_name("SCRIPTWRITER_MASTER_PROMPT.md")

    if not prompt_path.is_file():
        raise RuntimeError(
            "SCRIPTWRITER_MASTER_PROMPT.md is missing. "
            "Restore the prompt file from the project package before running."
        )

    prompt = prompt_path.read_text(encoding="utf-8").strip()

    if "[[TOPIC]]" not in prompt:
        raise RuntimeError(
            "SCRIPTWRITER_MASTER_PROMPT.md must contain the [[TOPIC]] placeholder."
        )

    brief = str(research_brief or "").strip()
    if not brief:
        brief = (
            "(No external source material was supplied for this run. Keep the "
            "narration to widely established, uncontroversial knowledge and avoid "
            "specific statistics, dates and quotations.)"
        )

    brief = (
        brief
        + "\n\nEVIDENCE RULES: every factual sentence must be supported by the "
        "material above. Do not state a percentage, a share, a date, a number or "
        "a superlative (largest, most, only) unless it appears in the material. "
        "A claim outside the material is treated as unverified and the script is "
        "rejected, so describe mechanisms instead of inventing figures."
    )

    replacements = {
        "[[TOPIC]]": str(topic or "").strip(),
        "[[LANGUAGE]]": LANGUAGE["name"],
        "[[LANGUAGE_NATIVE]]": LANGUAGE["native_name"],
        "[[RESEARCH_BRIEF]]": brief,
    }
    for token, value in replacements.items():
        prompt = prompt.replace(token, value)
    return prompt


def extract_json_object(raw_text):
    """Extract the first complete JSON object, including fenced responses."""
    if isinstance(raw_text, dict):
        return raw_text

    raw = str(raw_text or "").strip()

    raw = re.sub(
        r"^```(?:json)?\s*",
        "",
        raw,
        flags=re.IGNORECASE
    )

    raw = re.sub(r"\s*```$", "", raw)

    start = -1
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(raw):
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
            if depth == 0:
                start = index
            depth += 1

        elif char == "}":
            if depth > 0:
                depth -= 1

                if depth == 0 and start >= 0:
                    return json.loads(
                        raw[start:index + 1]
                    )

    raise ValueError(
        "Provider response did not contain a complete JSON object."
    )


def post_with_transient_retry(url, retries=1, **kwargs):
    """Retry temporary network/server failures; never retry HTTP 429."""
    last_error = None
    retries = max(0, int(retries))

    for attempt in range(retries + 1):
        try:
            response = requests.post(url, **kwargs)

            if response.status_code == 429:
                return response

            if response.status_code in (408, 500, 502, 503, 504):
                last_error = RuntimeError(
                    f"Temporary HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

                if attempt < retries:
                    print(
                        "Temporary AI service issue; retrying once in 5s..."
                    )
                    time.sleep(5)
                    continue

            return response

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_error = exc

            if attempt < retries:
                print(
                    "AI connection issue; retrying once in 5s..."
                )
                time.sleep(5)
                continue

    if last_error:
        raise last_error

    raise RuntimeError(
        "AI request failed without a response."
    )


def call_openai_compatible(provider, api_key, model, prompt):
    if provider == "Groq":
        url = (
            "https://api.groq.com/openai/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    else:
        url = (
            "https://openrouter.ai/api/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv(
                "OPENROUTER_SITE_URL",
                "https://github.com/"
            ),
            "X-Title": "Auto Documentary Video",
        }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert documentary writer and visual "
                    "producer. Follow the requested schema exactly. "
                    "Return only one valid JSON object, with no markdown."
                ),
            },
            {
                "role": "user",
                "content": prompt
            },
        ],
        "temperature": 0.35,
    }

    if provider == "Groq":
        payload["max_completion_tokens"] = 6000
    else:
        payload["max_tokens"] = 6000

    response = post_with_transient_retry(
        url,
        headers=headers,
        json=payload,
        timeout=(20, 180),
    )
    _observe_provider_response(provider, response, api_key=api_key, model=model)

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")

        raise RuntimeError(
            f"{provider} HTTP {response.status_code}: {detail}"
        )

    data = response.json()
    if provider == "OpenRouter":
        QUOTA_ROUTER.invalidate_probe(_quota_credential_id(provider, api_key))

    try:
        return extract_json_object(
            data["choices"][0]["message"]["content"]
        )

    except Exception as exc:
        raise RuntimeError(
            f"{provider} returned an unreadable response: {exc}; "
            f"body={str(data)[:700]}"
        ) from exc


def call_gemini(api_key, model, prompt):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.55,
        },
    }

    response = post_with_transient_retry(
        url,
        params={"key": api_key},
        json=payload,
        timeout=(20, 180),
    )
    _observe_provider_response("Gemini", response, api_key=api_key)

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")

        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: {detail}"
        )

    data = response.json()

    try:
        return extract_json_object(
            data["candidates"][0]["content"]["parts"][0]["text"]
        )

    except Exception as exc:
        raise RuntimeError(
            f"Gemini returned an unreadable response: {exc}; "
            f"body={str(data)[:700]}"
        ) from exc


def call_cloudflare_chat(api_token, account_id, model, prompt):
    """Use Cloudflare Workers AI's OpenAI-compatible chat endpoint."""
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{account_id}/ai/v1/chat/completions"
    )
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert documentary writer and visual producer. "
                    "Follow the requested schema exactly. Return only one valid JSON object."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_tokens": 6000,
    }
    response = post_with_transient_retry(
        url,
        retries=1,
        headers=headers,
        json=payload,
        timeout=(20, 180),
    )
    _observe_provider_response("Cloudflare", response, account_id=account_id)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Cloudflare AI HTTP {response.status_code}: "
            f"{response.text[:700].replace(chr(10), ' ')}"
        )
    try:
        data = response.json()
        return extract_json_object(data["choices"][0]["message"]["content"])
    except Exception as exc:
        raise RuntimeError(
            f"Cloudflare AI returned an unreadable response: {exc}; "
            f"body={response.text[:700]}"
        ) from exc


def call_json_with_providers(prompt, label="auxiliary request"):
    """Ask any available free provider for one JSON object.

    Used by the fact-check and script-repair stages. Routes are rebuilt each call
    so quota-aware ranking and key rotation still apply.
    """
    routes = []
    for key_index, api_key in enumerate(GROQ_KEYS, 1):
        routes.append(_quota_route(
            "Groq", f"Groq key {key_index}", api_key,
            lambda api_key=api_key: call_openai_compatible("Groq", api_key, GROQ_MODEL, prompt),
        ))
    for key_index, api_key in enumerate(OPENROUTER_KEYS, 1):
        routes.append(_quota_route(
            "OpenRouter", f"OpenRouter key {key_index}", api_key,
            lambda api_key=api_key: call_openai_compatible("OpenRouter", api_key, OPENROUTER_MODEL, prompt),
            model=OPENROUTER_MODEL,
        ))
    for key_index, api_key in enumerate(GEMINI_KEYS, 1):
        routes.append(_quota_route(
            "Gemini", f"Gemini key {key_index}", api_key,
            lambda api_key=api_key: call_gemini(api_key, MODEL, prompt),
            route_id=f"Gemini::{QuotaAwareRouter.fingerprint(api_key)}",
        ))
    for key_index, credentials in enumerate(CLOUDFLARE_CREDENTIALS, 1):
        routes.append(_quota_route(
            "Cloudflare", f"Cloudflare AI key pair {key_index}", credentials["token"],
            lambda credentials=credentials: call_cloudflare_chat(
                credentials["token"], credentials["account_id"], CLOUDFLARE_CHAT_MODEL, prompt
            ),
            account_id=credentials["account_id"],
        ))

    if not routes:
        raise RuntimeError(
            f"No AI provider is configured for the {label}."
        )

    errors = []
    pending = list(routes)
    while pending:
        ranked = QUOTA_ROUTER.rank_routes(pending)
        if not ranked:
            break
        route = ranked[0]
        pending.remove(route)
        QUOTA_ROUTER.mark_selected(route)
        try:
            return route["run"](), route["name"]
        except Exception as exc:
            errors.append(f"{route['name']}: {exc}")
            print(f"{label}: {errors[-1]}")
    raise RuntimeError(
        f"All configured AI providers failed during the {label}:\n- " + "\n- ".join(errors)
    )


def gemini_plan(topic, research_brief=None):
    """Generate the documentary plan through live quota-aware provider ranking."""
    prompt = documentary_prompt(topic, research_brief=research_brief)
    routes = []

    for key_index, api_key in enumerate(GROQ_KEYS, 1):
        routes.append(_quota_route(
            "Groq", f"Groq key {key_index}", api_key,
            lambda api_key=api_key: call_openai_compatible("Groq", api_key, GROQ_MODEL, prompt),
        ))

    for key_index, api_key in enumerate(OPENROUTER_KEYS, 1):
        routes.append(_quota_route(
            "OpenRouter", f"OpenRouter key {key_index}", api_key,
            lambda api_key=api_key: call_openai_compatible("OpenRouter", api_key, OPENROUTER_MODEL, prompt),
            model=OPENROUTER_MODEL,
        ))

    for key_index, api_key in enumerate(GEMINI_KEYS, 1):
        routes.append(_quota_route(
            "Gemini", f"Gemini key {key_index}", api_key,
            lambda api_key=api_key: call_gemini(api_key, MODEL, prompt),
            route_id=f"Gemini::{QuotaAwareRouter.fingerprint(api_key)}",
        ))

    for key_index, credentials in enumerate(CLOUDFLARE_CREDENTIALS, 1):
        routes.append(_quota_route(
            "Cloudflare", f"Cloudflare AI key pair {key_index}", credentials["token"],
            lambda credentials=credentials: call_cloudflare_chat(
                credentials["token"], credentials["account_id"], CLOUDFLARE_CHAT_MODEL, prompt
            ),
            account_id=credentials["account_id"],
        ))

    if not routes:
        raise RuntimeError(
            "No AI provider API key found. Configure at least one Groq, "
            "OpenRouter, Gemini, or Cloudflare AI token/account pair."
        )

    errors = []
    plan = None
    pending = list(routes)

    while pending:
        ranked = QUOTA_ROUTER.rank_routes(pending)
        if not ranked:
            break
        route = ranked[0]
        pending.remove(route)
        QUOTA_ROUTER.mark_selected(route)
        provider_name = route["name"]
        print(f"Quota-aware selection: {provider_name} (best currently known capacity).")

        try:
            candidate = validate_plan_candidate(route["run"]())
            plan = candidate
            print(f"Documentary plan generated successfully by {provider_name}.")
            break
        except Exception as exc:
            errors.append(f"{provider_name}: {exc}")
            print(errors[-1])
            if pending:
                print("Rechecking provider availability and ranking the remaining routes...")

    if plan is None:
        if not errors:
            errors.append("All routes are exhausted, cooling down, or unauthorized.")
        raise RuntimeError("All configured AI providers failed:\n- " + "\n- ".join(errors))

    Path("plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Script ready: {len(plan['script'].split())} words, {len(plan['scenes'])} scenes.")
    return plan


def validate_plan_candidate(candidate):
    """Validate a generated/repaired/expanded plan and map its sentences.

    Shared by the initial plan, the fact-check repair pass and the duration
    recovery pass so every plan obeys the same format invariants.
    """
    if not isinstance(candidate, dict):
        raise RuntimeError("Plan response is not a JSON object.")
    candidate["scenes"] = candidate.get("scenes", [])
    # MM7: the scene count follows the story. Four to twelve scenes are accepted;
    # no scene may be added just to satisfy a number.
    if (
        not isinstance(candidate["scenes"], list)
        or not MIN_PLAN_SCENES <= len(candidate["scenes"]) <= MAX_PLAN_SCENES
    ):
        raise RuntimeError(
            "AI returned an unusable plan. Expected "
            f"{MIN_PLAN_SCENES}-{MAX_PLAN_SCENES} scenes that follow the story, got "
            f"{len(candidate['scenes']) if isinstance(candidate['scenes'], list) else 'no list'}."
        )

    scene_narrations = []
    for scene_index, scene in enumerate(candidate["scenes"], 1):
        if not isinstance(scene, dict):
            raise RuntimeError(f"Scene {scene_index} is not a JSON object.")
        narration = re.sub(r"\s+", " ", str(scene.get("narration", ""))).strip()
        if not narration:
            raise RuntimeError(f"Scene {scene_index} has empty narration.")
        scene["narration"] = narration
        scene_narrations.append(narration)

    candidate["script"] = " ".join(scene_narrations).strip()
    word_count = len(candidate["script"].split())
    if not 120 <= word_count <= 200:
        raise RuntimeError(f"Scene narration totals {word_count} words; expected 120-200.")
    if not reference_style.narration_language_matches(candidate["script"], SCRIPT_LANGUAGE):
        raise RuntimeError(
            f"Narration is not written in {LANGUAGE['name']}. "
            "The provider ignored the requested narration language."
        )

    hook = str(candidate.get("hook") or "").strip()
    if not hook:
        hook = split_sentences(candidate["script"])[0] if candidate["script"] else ""
        candidate["hook"] = hook
    hook_words = str(hook).split()
    if len(hook_words) > 12:
        # Keep the strongest opening clause but never invent a new sentence.
        candidate["hook"] = " ".join(hook_words[:12]).rstrip(",;:-") + "..."

    candidate["scenes"][0]["visual_type"] = "footage"
    assign_scene_sentence_ranges(candidate)
    return candidate


def _research_sources(research_data):
    return [
        {"title": article.get("title"), "url": article.get("url")}
        for article in (research_data or {}).get("articles") or []
    ]


def _verify_claims(script, claims, research_data, label="fact check"):
    sources = _research_sources(research_data)
    prompt = research_module.fact_check_prompt(
        script, claims, research_data.get("brief", ""), sources=sources
    )
    payload, provider = call_json_with_providers(prompt, label)
    result = research_module.summarise_verification(payload, claims, sources=sources)
    result["provider"] = provider
    result["research_articles"] = sources
    return result


# Scene counts are derived from the story, never padded to a quota.
MIN_PLAN_SCENES = 4
MAX_PLAN_SCENES = 12

# A fact-check that checked nothing is a failure, not a pass.
MIN_FACT_CHECK_CLAIMS = 3
# A legitimately atmospheric script can have no factual claim at all, but that
# is a different outcome from the extractor failing to read the narration.
NO_CLAIMS_COVERAGE = 0.8


def analyse_script_claims(script):
    """Classify every narration sentence as a factual claim or non-factual.

    The semantic extractor is asked to account for the whole script. Sentences it
    leaves out are recorded as skipped and then re-read by the sentence-level
    analyser, so a claim can never disappear from verification silently. The
    heuristic path is only used when no provider is reachable.
    """
    script = str(script or "").strip()
    analysis = {
        "claims": [],
        "non_factual": [],
        "source": "none",
        "provider": None,
        "coverage": research_module.claim_coverage(script, [], []),
        "skipped_sentences": [],
        "error": None,
    }
    if not script:
        return analysis

    try:
        payload, provider = call_json_with_providers(
            research_module.claims_prompt(script), "claim extraction"
        )
        semantic = research_module.parse_claim_analysis(payload)
        if semantic["claims"] or semantic["non_factual"]:
            analysis.update(
                claims=semantic["claims"],
                non_factual=semantic["non_factual"],
                source=f"semantic:{provider}",
                provider=provider,
            )
            analysis["coverage"] = research_module.claim_coverage(
                script, analysis["claims"], analysis["non_factual"]
            )
            uncovered = list(analysis["coverage"].get("uncovered_sentences") or [])
            if uncovered:
                # Sentences the extractor skipped are still part of the script.
                # Re-read them individually instead of letting them vanish.
                extra = research_module.extract_claims(" ".join(uncovered))
                known = {claim.lower() for claim in analysis["claims"]}
                # Every non-question statement in the uncovered set is treated as
                # a claim: verifying too much is safe, dropping a claim is not.
                extra.extend(
                    sentence for sentence in uncovered
                    if len(sentence.split()) >= 4
                    and not sentence.rstrip().endswith("?")
                )
                added = []
                for claim in extra:
                    if claim.lower() in known:
                        continue
                    known.add(claim.lower())
                    added.append(claim)
                if added:
                    analysis["claims"].extend(added)
                    print(
                        f"Claim extraction skipped {len(uncovered)} sentence(s); "
                        f"{len(added)} were recovered as claims for verification."
                    )
                analysis["skipped_sentences"] = [
                    sentence for sentence in uncovered
                    if sentence.lower() not in {claim.lower() for claim in analysis["claims"]}
                ]
                analysis["coverage"] = research_module.claim_coverage(
                    script, analysis["claims"], analysis["non_factual"]
                )
            return analysis
        analysis["error"] = "The semantic extractor returned no sentence classification."
        print("Claim extraction returned no usable classification; falling back to sentence-level analysis.")
    except Exception as exc:
        analysis["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        print(
            "Semantic claim extraction unavailable "
            f"({str(exc)[:200]}); using sentence-level analysis."
        )

    heuristic = research_module.extract_claims(script)
    analysis.update(claims=heuristic, non_factual=[], source="heuristic")
    analysis["coverage"] = research_module.claim_coverage(script, heuristic, [])
    analysis["skipped_sentences"] = list(
        analysis["coverage"].get("uncovered_sentences") or []
    )
    return analysis


def extract_script_claims(script):
    """Backwards-compatible wrapper: semantic claims plus their provenance."""
    analysis = analyse_script_claims(script)
    return analysis["claims"], analysis["source"]


def _fail_fact_check(research_data, status, message, analysis=None, verification=None):
    """Record the failed fact-check state in fact_check.json before aborting.

    The claims and their verdicts are kept. An earlier version wrote an empty
    claim list here, so the only place that said *which* claim was unsupported
    was a log line nobody without account access could read.
    """
    analysis = analysis or {}
    verification = verification or {}
    payload = {
        "status": status,
        "overall": "fail",
        "claims": list(verification.get("claims") or []),
        "checked": int(verification.get("checked") or 0),
        "supported": int(verification.get("supported") or 0),
        "unsupported": int(verification.get("unsupported") or 0),
        "contradicted": int(verification.get("contradicted") or 0),
        "rejected_claims": [
            {
                "claim": item.get("claim"),
                "verdict": item.get("verdict"),
                "evidence": item.get("evidence"),
            }
            for item in (verification.get("claims") or [])
            if item.get("verdict") != "supported"
        ],
        "claim_source": analysis.get("source"),
        "coverage": analysis.get("coverage"),
        "skipped_sentences": analysis.get("skipped_sentences") or [],
        "error": analysis.get("error"),
        "research_articles": _research_sources(research_data),
    }
    try:
        research_module.write_research_files(research_data, payload)
    except Exception as exc:
        print(f"Could not persist the failed fact-check report: {exc}")
    raise RuntimeError(message)


def _retrieve_evidence_for_rejected_claims(plan, verification, research_data, claims):
    """Look up the passages the rejected claims need, then re-verify.

    Rewriting a script is not research. When a claim is unsupported, the honest
    next step is to find the passage that actually discusses it and re-run the
    same evidence-bound verifier against that enriched material. If nothing new
    is found, the original verdicts stand and the repair path continues.
    """
    rejected = [
        item.get("claim") for item in (verification.get("claims") or [])
        if item.get("verdict") in ("unsupported", "contradicted") and item.get("claim")
    ]
    if not rejected:
        return verification

    languages = []
    topic_language = str(research_data.get("topic_language") or "").strip()
    for language in (topic_language, SCRIPT_LANGUAGE, "en"):
        if language and language not in languages:
            languages.append(language)

    try:
        retrieval = research_module.gather_claim_evidence(
            rejected,
            languages=languages,
            topic_terms=research_data.get("topic_terms") or [],
        )
    except Exception as exc:
        print(f"Claim evidence retrieval failed: {str(exc)[:300]}")
        return verification

    if not retrieval.get("brief"):
        print(
            "Claim evidence retrieval found no new passage for "
            f"{len(rejected)} rejected claim(s); keeping the original verdicts."
        )
        return verification

    existing = str(research_data.get("brief") or "")
    research_data["brief"] = (existing + "\n\n" + retrieval["brief"]).strip()
    research_data.setdefault("articles", []).extend(retrieval.get("articles") or [])
    research_data["claim_evidence"] = {
        "queries": retrieval.get("queries") or [],
        "characters": retrieval.get("characters") or 0,
        "sources": [
            {"title": article.get("title"), "url": article.get("url")}
            for article in retrieval.get("articles") or []
        ],
    }
    print(
        "Retrieved "
        f"{len(retrieval.get('articles') or [])} additional source(s) for "
        f"{len(rejected)} rejected claim(s); re-verifying against the enriched brief."
    )
    rechecked = _verify_claims(
        plan["script"], claims, research_data, label="fact check with retrieved evidence"
    )
    rechecked["evidence_retrieval"] = research_data["claim_evidence"]
    return rechecked


def fact_check_plan(plan, research_data, max_repairs=2):
    """Verify every factual claim and repair contradicted/unsupported wording.

    Requirements enforced here:
    * each claimant gets a verdict, evidence and (when available) the cited
      source title/URL stored in fact_check.json;
    * a script with no factual claim is reported separately from an extraction
      failure, and neither can be turned into a PASS;
    * sentences skipped by the extractor are recorded, never dropped;
    * unsupported or contradicted wording is repaired and re-verified, and the
      run aborts rather than shipping unverified factual claims.
    """
    analysis = analyse_script_claims(plan.get("script", ""))
    claims = list(analysis["claims"])
    claim_source = analysis["source"]
    coverage = analysis.get("coverage") or {}

    if not claims:
        script_sentences = int(coverage.get("script_sentences") or 0)
        non_factual = analysis.get("non_factual") or []
        looks_classified = (
            bool(non_factual)
            and float(coverage.get("coverage") or 0.0) >= NO_CLAIMS_COVERAGE
            and script_sentences > 0
        )
        if looks_classified:
            _fail_fact_check(
                research_data,
                "no_factual_claims",
                "Fact check: the script contains no verifiable factual claim "
                f"({len(non_factual)} sentence(s) classified as non-factual via "
                f"{claim_source}). This is a legitimate no-claim narration, so the "
                "run stops instead of issuing a PASS with nothing checked.",
                analysis,
            )
        _fail_fact_check(
            research_data,
            "extraction_failed",
            "Fact check failed to extract claims from the script "
            f"(source={claim_source}, error={analysis.get('error')}). No claim was "
            "verified, so no PASS is possible.",
            analysis,
        )

    if len(claims) < MIN_FACT_CHECK_CLAIMS:
        _fail_fact_check(
            research_data,
            "insufficient_claims",
            "Fact check could not identify at least "
            f"{MIN_FACT_CHECK_CLAIMS} verifiable claims in the script "
            f"(found {len(claims)} via {claim_source}). A PASS without checked "
            "claims is not allowed; the script must be regenerated with concrete, "
            "verifiable statements.",
            analysis,
        )

    verification = _verify_claims(plan["script"], claims, research_data)
    verification = _retrieve_evidence_for_rejected_claims(plan, verification, research_data, claims)
    verification["claim_source"] = claim_source
    verification["coverage"] = coverage
    verification["skipped_sentences"] = analysis.get("skipped_sentences") or []
    verification["status"] = "checked"
    print(
        f"Fact check: {verification['supported']} supported "
        f"({verification.get('sourced_supports', 0)} with a cited source), "
        f"{verification['unsupported']} unsupported, "
        f"{verification['contradicted']} contradicted "
        f"(overall {verification['overall']}); sentence coverage "
        f"{float(coverage.get('coverage') or 0.0):.0%}."
    )

    repairs = 0
    while verification["overall"] != "pass" and repairs < max_repairs:
        repairs += 1
        problems = "\n".join(
            f"- {item['claim']} ({item['verdict']}): {item['evidence']}"
            for item in verification["claims"]
            if item["verdict"] != "supported"
        )
        scene_count = len(plan.get("scenes") or [])
        prompt = (
            "You are correcting a documentary script so that every factual claim "
            "is supported. Rewrite only what is necessary; keep the same story "
            "and the same visual queries. Remove, correct or soften each "
            "problem claim. Do not add new numbers, dates or quotations that the "
            "source material does not contain.\n\n"
            "RULES FOR THE REWRITE:\n"
            "- A contradicted claim is wrong. Never restate it in any form, and do "
            "not replace it with a similar statistic.\n"
            "- Do not introduce any new number, share, percentage, date or "
            "superlative (largest, biggest, most, only) unless those exact words "
            "appear in the source material. Describe the mechanism instead.\n"
            "- Deleting a sentence is allowed. Replacing a removed claim with "
            "another unverifiable dramatic line is not: the word count target is "
            "secondary to correctness.\n"
            "- Keep at least three separate sentences that make a checkable "
            "statement the source material supports.\n\n"
            f"SOURCE MATERIAL:\n{research_data.get('brief') or '(none supplied)'}\n\n"
            f"CURRENT PLAN JSON:\n{json.dumps(plan, ensure_ascii=False)[:9000]}\n\n"
            f"PROBLEMS:\n{problems}\n\n"
            "Return ONLY the same JSON object schema: title, hook, script, and "
            f"exactly {scene_count} scenes each with narration, visual_query, "
            "visual_queries, source_label, source_url. The combined narration must "
            f"stay between 120 and 200 words and must be written in {LANGUAGE['name']}."
        )
        try:
            payload, provider = call_json_with_providers(prompt, "fact-check repair")
            repaired = validate_plan_candidate(payload)
        except Exception as exc:
            print(f"Fact-check repair failed: {str(exc)[:400]}")
            break
        plan.clear()
        plan.update(repaired)
        repaired_analysis = analyse_script_claims(plan["script"])
        repaired_claims = repaired_analysis["claims"]
        if len(repaired_claims) < MIN_FACT_CHECK_CLAIMS:
            _fail_fact_check(
                research_data,
                "insufficient_claims_after_repair",
                "The repaired script no longer contains enough verifiable claims "
                f"({len(repaired_claims)} via {repaired_analysis['source']}); "
                "refusing to report a fact-check pass.",
                repaired_analysis,
            )
        verification = _verify_claims(
            plan["script"],
            repaired_claims,
            research_data,
            label="fact-check re-verification",
        )
        verification["claim_source"] = repaired_analysis["source"]
        verification["coverage"] = repaired_analysis.get("coverage")
        verification["skipped_sentences"] = repaired_analysis.get("skipped_sentences") or []
        verification["repair_provider"] = provider
        verification["repair_attempt"] = repairs
        verification["status"] = "checked"
        print(
            f"Fact check after repair {repairs}: {verification['supported']} supported, "
            f"{verification['unsupported']} unsupported, "
            f"{verification['contradicted']} contradicted."
        )

    contradicted = [
        item for item in verification["claims"]
        if item["verdict"] == "contradicted"
    ]
    if contradicted:
        _fail_fact_check(
            research_data,
            "contradicted",
            "Fact check found claims contradicted by the source material after "
            "repair: " + " | ".join(
                f"{item['claim']} ({item['evidence']})" for item in contradicted
            ),
            analysis,
            verification,
        )

    unsupported = [
        item for item in verification["claims"]
        if item["verdict"] == "unsupported"
    ]
    if unsupported:
        allow = os.getenv("ALLOW_UNSUPPORTED_CLAIMS", "").strip().lower() in (
            "1", "true", "yes", "on"
        )
        flagged = [
            {"claim": item["claim"], "evidence": item["evidence"]}
            for item in unsupported
        ]
        verification["flagged_claims"] = flagged
        if not allow:
            _fail_fact_check(
                research_data,
                "unsupported",
                "Fact check could not support "
                f"{len(unsupported)} claim(s) after {max_repairs} repair attempt(s), "
                "so the script is not safe to narrate: "
                + " | ".join(item["claim"] for item in unsupported[:6])
                + ". Set ALLOW_UNSUPPORTED_CLAIMS=1 only to keep such wording with an "
                "explicit flag in fact_check.json.",
                analysis,
                verification,
            )
        print(
            "Fact-check warning: ALLOW_UNSUPPORTED_CLAIMS is set; "
            f"{len(unsupported)} unsupported claim(s) stay in the script and are "
            "flagged in fact_check.json."
        )

    return verification


def extend_plan_for_duration(plan, research_brief, target_words=185):
    """Ask the writer to deepen the script so the spoken VO reaches 70s.

    Used only when the measured voice-over is too short; the result is fully
    re-validated (8 scenes, 120-200 words, target language).
    """
    prompt = (
        "A documentary voice-over measured shorter than the required 70 seconds. "
        "Deepen the narration so it reads at roughly 185 words while staying "
        "between 120 and 200 words. Add concrete, source-supported detail, a "
        "clearer mechanism, or a stronger transition; never add filler, "
        "repetition, or unsupported numbers. Keep the current number of scenes "
        "(never add a scene just to lengthen the video), keep every visual query "
        "usable, and keep the hook at 12 words or fewer.\n\n"
        f"SOURCE MATERIAL:\n{research_brief or '(none supplied)'}\n\n"
        f"CURRENT PLAN JSON:\n{json.dumps(plan, ensure_ascii=False)[:9000]}\n\n"
        "Return ONLY the same JSON object schema: title, hook, script, and the "
        f"same {len(plan.get('scenes') or [])} scenes each with narration, "
        "visual_query, visual_queries, source_label, source_url. Narration "
        f"language: {LANGUAGE['name']}."
    )
    payload, provider = call_json_with_providers(prompt, "duration extension")
    extended = validate_plan_candidate(payload)

    # The footage for each scene was already downloaded and visually verified
    # against the original narration, so keep both the approved media and its
    # visual intent when the wording is deepened.
    carry_over = (
        "media_file", "visual_type", "selected_footage_source",
        "selected_footage_tags", "selected_footage_url", "source", "creator",
        "page_url", "license", "coverr_attribution", "source_video_dimensions",
        "smart_vertical_reframe", "reused_footage_from_scene",
        "footage_search_retry_count", "footage_search_retry_queries",
    )
    for old_scene, new_scene in zip(plan.get("scenes", []), extended["scenes"]):
        for key in carry_over:
            if key in old_scene:
                new_scene[key] = old_scene[key]
        if old_scene.get("visual_query"):
            new_scene["visual_query"] = old_scene["visual_query"]
        if old_scene.get("visual_queries"):
            new_scene["visual_queries"] = old_scene["visual_queries"]
    print(
        f"Duration recovery: script expanded to "
        f"{len(extended['script'].split())} words by {provider}."
    )
    return extended


def assign_scene_sentence_ranges(plan):
    """Map each scene to exact script sentences."""
    sentences = split_sentences(plan["script"])

    normalized = [
        " ".join(normalize_words(s))
        for s in sentences
    ]

    cursor = 0

    for index, scene in enumerate(plan["scenes"]):
        narration = str(
            scene.get("narration", "")
        ).strip()

        target = " ".join(
            normalize_words(narration)
        )

        if not target:
            raise RuntimeError(
                f"Scene {index + 1} has no narration."
            )

        target_words = target.split()
        found = None

        for start in range(cursor, len(sentences)):
            joined = []

            for end in range(
                start,
                min(len(sentences), start + 5)
            ):
                joined.extend(
                    normalized[end].split()
                )

                if joined == target_words:
                    found = (start, end)
                    break

                if len(joined) >= len(target_words):
                    break

            if found:
                break

        if not found:
            raise RuntimeError(
                f"Scene {index + 1} narration is not an exact "
                "consecutive passage from the script. "
                "Regenerate the plan."
            )

        scene["sentence_start"] = found[0]
        scene["sentence_end"] = found[1]

        cursor = found[1] + 1

    if cursor != len(sentences):
        raise RuntimeError(
            "Some script sentences are not assigned to a scene."
        )


# ============================================================
# FOOTAGE SEARCH AND RELEVANCE
# ============================================================

def download_stream(url, dest, source=None, max_bytes=None):
    """Stream a candidate to disk with adaptive byte limits and HTTP Range resume."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    byte_limit = int(max_bytes or MAX_CLIP_BYTES)
    if byte_limit <= 0 or byte_limit > MAX_EXCEPTIONAL_CLIP_BYTES:
        raise ValueError("Download byte limit is outside the configured safety range.")

    is_commons = is_wikimedia_url(url)
    source = "Wikimedia Commons" if is_commons else (source or "Internet Archive")
    max_attempts = 3
    started_at = time.monotonic()
    dest.unlink(missing_ok=True)

    for attempt in range(1, max_attempts + 1):
        offset = part.stat().st_size if part.is_file() else 0
        headers = {
            "User-Agent": WIKIMEDIA_USER_AGENT if is_commons else (
                "AutoDocumentaryVideo/5.0 (+https://github.com/nikmatpertama-sys/auto-documentary-video)"
            )
        }
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"

        try:
            with _footage_source_get(
                source,
                url,
                stream=True,
                timeout=(25, 180),
                headers=headers,
            ) as response:
                status = int(getattr(response, "status_code", 0) or 0)
                if status == 429:
                    response.raise_for_status()

                if status == 416 and offset > 0:
                    part.unlink(missing_ok=True)
                    if attempt < max_attempts:
                        continue
                    raise RuntimeError("Server rejected the resume range; restart budget exhausted.")

                if status in (408, 425) or 500 <= status <= 599:
                    if attempt < max_attempts:
                        delay = response_retry_after_seconds(response, default_seconds=5 * attempt)
                        delay = min(30.0, max(3.0, delay))
                        print(
                            f"{source} returned HTTP {status}; retrying download in "
                            f"{delay:.0f}s ({attempt}/{max_attempts - 1})."
                        )
                        time.sleep(delay)
                        continue

                response.raise_for_status()
                if status == 206 and offset > 0:
                    content_range = str(response.headers.get("Content-Range") or "")
                    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.I)
                    if not match or int(match.group(1)) != offset:
                        part.unlink(missing_ok=True)
                        raise RuntimeError("Server returned an invalid Content-Range for resume.")
                    mode = "ab"
                    declared = int(match.group(3)) if match.group(3).isdigit() else 0
                else:
                    # A server that ignores Range returns 200; safely restart the partial file.
                    mode = "wb"
                    offset = 0
                    declared_length = int(response.headers.get("Content-Length") or 0)
                    declared = declared_length

                if declared and declared > byte_limit:
                    part.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Clip is {declared / 1024 / 1024:.1f} MB, above the "
                        f"{byte_limit / 1024 / 1024:.0f} MB relevance-based limit."
                    )

                disk_free = shutil.disk_usage(dest.parent).free
                if disk_free < DOWNLOAD_DISK_RESERVE_BYTES + min(byte_limit, declared or byte_limit):
                    raise RuntimeError(
                        "Insufficient safe disk headroom for this footage download; "
                        "the generator is preserving space for models, audio and rendering."
                    )

                total = offset
                with open(part, mode) as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        if time.monotonic() - started_at > DOWNLOAD_MAX_SECONDS:
                            raise RuntimeError(
                                f"Footage download exceeded the {DOWNLOAD_MAX_SECONDS // 60}-minute time budget."
                            )
                        total += len(chunk)
                        if total > byte_limit:
                            part.unlink(missing_ok=True)
                            raise RuntimeError(
                                f"Clip exceeded its {byte_limit / 1024 / 1024:.0f} MB download budget."
                            )
                        if shutil.disk_usage(dest.parent).free < DOWNLOAD_DISK_RESERVE_BYTES:
                            raise RuntimeError(
                                "Download stopped to preserve the configured free-disk reserve."
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())

            if not part.is_file() or part.stat().st_size < 50000:
                raise RuntimeError("Downloaded clip is unexpectedly small.")
            part.replace(dest)
            return

        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status == 429:
                # Keep a valid partial file for a later Range resume after cooldown.
                raise
            transient = status in (None, 408, 425) or (
                isinstance(status, int) and 500 <= status <= 599
            )
            if transient and attempt < max_attempts:
                delay = min(20.0, 3.0 * (2 ** (attempt - 1)))
                print(
                    f"Temporary download error from {source}: {exc}; "
                    f"retrying/resuming in {delay:.0f}s ({attempt}/{max_attempts - 1})."
                )
                time.sleep(delay)
                continue
            if transient:
                mark_footage_source_temporarily_unavailable(source, 45.0)
            raise
        except Exception:
            # A connection interruption retains .part for the bounded retry; policy
            # and validation errors remove it so partial content cannot be mistaken for media.
            if isinstance(__import__("sys").exc_info()[1], (RuntimeError, ValueError)):
                message = str(__import__("sys").exc_info()[1]).lower()
                if any(token in message for token in ("above", "exceeded", "insufficient", "invalid content-range", "unexpectedly small")):
                    part.unlink(missing_ok=True)
            raise

    raise RuntimeError("Footage download exhausted its retry budget.")


def clean_query(query):
    return re.sub(
        r"[^a-zA-Z0-9 ]",
        " ",
        str(query)
    ).strip()[:95]


class _GoogleResultParser(HTMLParser):
    """Extract ordinary result links and their visible title text from Google HTML."""
    def __init__(self):
        super().__init__()
        self.results = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.results.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


def _google_direct_file_search(query):
    """Discover directly indexed reusable MP4 files through Google Search.

    Google is a discovery index, not a video host. Only direct video-file URLs
    carrying an explicit public-domain/CC reuse signal in the indexed result
    are admitted; ordinary YouTube/social pages are not scraped or downloaded.
    """
    query = clean_query(query)
    if not query:
        return []

    search_text = (
        f'{query} filetype:mp4 ("public domain" OR "Creative Commons" OR CC0 OR "CC BY")'
    )
    response = _footage_source_get(
        "Google Search",
        "https://www.google.com/search",
        params={"q": search_text, "num": 30, "filter": "0"},
        headers={
            "User-Agent": "AutoDocumentaryVideo/3.0 (+https://github.com/nikmatpertama-sys/auto-documentary-video)",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=30,
    )
    response.raise_for_status()
    parser = _GoogleResultParser()
    parser.feed(response.text)

    results = []
    seen = set()
    reuse_terms = re.compile(r"public\s*domain|creative\s*commons|\bcc0\b|\bcc\s*by\b", re.I)
    for raw_url, title in parser.results:
        if raw_url.startswith("/url?"):
            raw_url = parse_qs(urlparse(raw_url).query).get("q", [""])[0]
        if not raw_url.startswith(("http://", "https://")):
            continue
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or not parsed.path.lower().endswith((".mp4", ".m4v", ".mov", ".webm")):
            continue
        if raw_url in seen:
            continue
        # Google places useful license/context text near the result anchor,
        # often outside the anchor title itself. Inspect a bounded local slice.
        position = response.text.find(raw_url)
        nearby_html = response.text[max(0, position - 1200):position + 1800] if position >= 0 else title
        nearby_text = html.unescape(re.sub(r"<[^>]+>", " ", nearby_html))
        result_context = " ".join((title, nearby_text))
        if not reuse_terms.search(result_context):
            continue
        seen.add(raw_url)
        results.append({
            "source": "Google Search",
            "id": hashlib.sha256(raw_url.encode()).hexdigest()[:20],
            "url": raw_url,
            "width": 0,
            "height": 0,
            "duration": 0,
            "title": title,
            "metadata": result_context[:1200],
            "tags": title,
            "creator": "See source page",
            "page_url": raw_url,
            "license": "Google indexed result indicates public-domain/Creative Commons; verify source terms",
        })
        if len(results) >= 12:
            break
    if results:
        print(f"  Google Search: {len(results)} direct reusable-video file candidate(s)")
    return results


# Hosts whose media is published for reuse by the site owner or by statute.
_PUBLIC_MEDIA_HOSTS = (
    "nasa.gov", "noaa.gov", "usgs.gov", "nps.gov", "loc.gov", "si.edu",
    "commons.wikimedia.org", "archive.org", "esa.int", "esa.int",
    "pexels.com", "pixabay.com", "coverr.co", "videvo.net", "mixkit.co",
)
_REUSE_SIGNAL = re.compile(
    r"public\s*domain|creative\s*commons|\bcc0\b|\bcc[\s-]?by\b|free\s+to\s+use|"
    r"royalty[\s-]free|no\s+attribution\s+required",
    re.IGNORECASE,
)
_VIDEO_FILE_PATTERN = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:mp4|m4v|mov|webm)(?:\?[^\s\"'<>]*)?", re.IGNORECASE
)
_VIDEO_PAGE_PATTERN = re.compile(
    r"<(?:video|source)[^>]+src=[\"']([^\"']+)[\"']|"
    r"<meta[^>]+(?:property|name)=[\"'](?:og:video(?::secure_url|:url)?|"
    r"twitter:player:stream)[\"'][^>]+content=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def _decode_search_result_href(href):
    """Resolve Google redirect links and DuckDuckGo ``uddg`` wrappers."""
    href = str(href or "").strip()
    if href.startswith("/url?"):
        return parse_qs(urlparse(href).query).get("q", [""])[0]
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
        return parse_qs(parsed.query).get("uddg", [""])[0]
    if href.startswith("//"):
        return "https:" + href
    return href


def _web_result_pages(search_text, source="Web Search", limit=40):
    """Ordinary web result pages from Google, falling back to DuckDuckGo HTML.

    Google increasingly answers scripted requests with a consent/JavaScript page
    that contains no result links; DuckDuckGo's HTML endpoint is a free,
    no-key fallback that still exposes indexed pages. Only result *pages* are
    returned here; media URLs are resolved and licence-checked separately.
    """
    engines = (
        ("DuckDuckGo", "https://html.duckduckgo.com/html/", {"q": search_text}),
        ("Google", "https://www.google.com/search", {"q": search_text, "num": 30, "filter": "0"}),
    )
    for engine, endpoint, params in engines:
        try:
            response = _footage_source_get(
                source,
                endpoint,
                params=params,
                headers={
                    "User-Agent": "AutoDocumentaryVideo/7.0 (+https://github.com/nikmatpertama-sys/auto-documentary-video)",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=30,
            )
            response.raise_for_status()
        except (FootageSourceBudgetExceeded, FootageSourceCooldownActive):
            raise
        except Exception as exc:
            print(f"  {engine} web discovery unavailable: {str(exc)[:180]}")
            continue

        parser = _GoogleResultParser()
        parser.feed(response.text)
        pages = []
        seen = set()
        for href, title in parser.results:
            resolved = _decode_search_result_href(href)
            if not resolved.startswith("https://"):
                continue
            host = (urlparse(resolved).hostname or "").lower()
            if not host or any(
                blocked in host
                for blocked in ("google.", "gstatic.", "duckduckgo.", "youtube.", "youtu.be")
            ):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            pages.append((engine, resolved, title))
            if len(pages) >= limit:
                break
        print(f"  {engine} web discovery: {len(pages)} result page(s).")
        if pages:
            return pages
    return []


def _page_video_urls(page_url, page_html):
    urls = []
    for match in _VIDEO_PAGE_PATTERN.finditer(page_html):
        candidate = match.group(1) or match.group(2)
        if candidate:
            urls.append(candidate)
    urls.extend(_VIDEO_FILE_PATTERN.findall(page_html[:400000]))

    resolved = []
    seen = set()
    for raw in urls:
        absolute = urljoin(page_url, html.unescape(str(raw or "").strip()))
        if not absolute.startswith("https://"):
            continue
        if not re.search(r"\.(?:mp4|m4v|mov|webm)(?:\?|$)", absolute, re.IGNORECASE):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        resolved.append(absolute)
        if len(resolved) >= 4:
            break
    return resolved


def _page_license_note(page_url, page_text):
    """Return a licence statement only when the page actually grants reuse."""
    host = (urlparse(page_url).hostname or "").lower()
    if any(host == allowed or host.endswith("." + allowed) for allowed in _PUBLIC_MEDIA_HOSTS):
        return f"Published by {host} for reuse; verify page terms ({page_url})"
    match = _REUSE_SIGNAL.search(page_text)
    if match:
        return f"Page states reusable licence ('{match.group(0)}'); verify page terms ({page_url})"
    return ""


def google_open_web_video_search(query):
    """Discover directly indexed reusable MP4 files through Google Search.

    When the precise direct-file path finds little, page-embedded video sources
    are added by ``web_page_video_search`` through the separate web-page source.
    """
    query = clean_query(query)
    if not query:
        return []

    try:
        results = _google_direct_file_search(query)
    except (FootageSourceBudgetExceeded, FootageSourceCooldownActive) as exc:
        print(f"  Google Search skipped: {str(exc)[:160]}")
        return []

    if len(results) >= 8:
        return results[:12]
    return _web_page_video_candidates(query, results)


def web_page_video_search(query):
    """Discover reusable video files embedded in ordinary web pages.

    Search engines index *pages* that host footage (stock libraries, agency
    media galleries, university and museum sites), not only bare .mp4 files.
    Those pages are fetched and their embedded video sources (``<video>``,
    ``og:video`` and .mp4 links) are extracted. A page is only used when it
    carries an explicit reuse licence or belongs to a public-media host, so web
    discovery never turns into "download whatever a search result shows".
    """
    return _web_page_video_candidates(clean_query(query), [])


def _web_page_video_candidates(query, results):
    """Append page-embedded video candidates discovered through web search."""
    if not query:
        return results
    seen_urls = {item["url"] for item in results}
    search_text = (
        f'{query} video ("creative commons" OR "public domain" OR CC0 OR "CC BY" '
        'OR "free stock footage")'
    )
    try:
        pages = _web_result_pages(search_text, source="Web Search", limit=30)
    except (FootageSourceBudgetExceeded, FootageSourceCooldownActive) as exc:
        print(f"  Web page discovery stopped: {str(exc)[:160]}")
        return results

    for engine, page_url, title in pages:
        if len(results) >= 12:
            break
        try:
            page = _footage_source_get(
                "Web Search",
                page_url,
                headers={
                    "User-Agent": "AutoDocumentaryVideo/7.0 (+https://github.com/nikmatpertama-sys/auto-documentary-video)",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=25,
            )
            page.raise_for_status()
        except (FootageSourceBudgetExceeded, FootageSourceCooldownActive) as exc:
            print(f"  Web page discovery stopped: {str(exc)[:160]}")
            break
        except Exception as exc:
            print(f"  Web page skipped ({page_url[:80]}): {str(exc)[:120]}")
            continue

        content_type = str(page.headers.get("Content-Type") or "").lower()
        if "html" not in content_type and "xml" not in content_type:
            continue
        page_html = page.text[:500000]
        video_urls = _page_video_urls(page_url, page_html)
        if not video_urls:
            continue
        page_text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page_html)))[:60000]
        license_note = _page_license_note(page_url, page_text)
        if not license_note:
            print(f"  Web page has video but no reuse licence stated: {page_url[:100]}")
            continue

        page_title = title
        if not page_title and "<title>" in page_html:
            page_title = page_html.split("<title>", 1)[1].split("</title>", 1)[0].strip()
        for video_url in video_urls[:2]:
            if video_url in seen_urls:
                continue
            seen_urls.add(video_url)
            results.append({
                "source": "Web Search",
                "id": hashlib.sha256(video_url.encode()).hexdigest()[:20],
                "url": video_url,
                "width": 0,
                "height": 0,
                "duration": 0,
                "title": page_title or page_url,
                "metadata": f"{page_title or ''} {title} {page_text[:900]}",
                "tags": f"{page_title or ''} {title}",
                "creator": "See source page",
                "page_url": page_url,
                "license": license_note,
                "engine": engine,
            })
        print(f"  Web page video source accepted: {page_url[:100]} ({len(video_urls)} file(s))")

    return results[:12]


def pixabay_search(query):
    if not PIXABAY_KEY:
        return []

    query = clean_query(query)

    cache_file = CACHE / (
        hashlib.sha256(
            query.lower().encode()
        ).hexdigest() + ".json"
    )

    if (
        cache_file.exists()
        and time.time() - cache_file.stat().st_mtime < 86400
    ):
        try:
            return json.loads(
                cache_file.read_text(encoding="utf-8")
            ).get("hits", [])
        except Exception:
            pass

    response = _footage_source_get(
        "Pixabay",
        "https://pixabay.com/api/videos/",
        params={
            "key": PIXABAY_KEY,
            "q": query,
            "per_page": 25,
            "safesearch": "true",
            "video_type": "film",
            "order": "popular"
        },
        timeout=35
    )

    if response.status_code == 429:
        raise RuntimeError(
            "Pixabay rate limit reached."
        )

    response.raise_for_status()

    data = response.json()

    cache_file.write_text(
        json.dumps(data),
        encoding="utf-8"
    )

    results = []

    for hit in data.get("hits", []):
        renditions = hit.get("videos", {})

        available_renditions = [
            rendition for rendition in renditions.values()
            if isinstance(rendition, dict) and rendition.get("url")
        ]
        available_renditions.sort(
            key=lambda item: (
                0 if is_native_portrait_916(
                    item.get("width") or 0, item.get("height") or 0
                ) else 1,
                0 if min(
                    int(item.get("width") or 0),
                    int(item.get("height") or 0),
                ) >= MIN_CLIP_SHORT_SIDE else 1,
                -int(item.get("height") or 0),
            )
        )
        rendition = available_renditions[0] if available_renditions else {}

        if not rendition.get("url"):
            continue

        results.append({
            "source": "Pixabay",
            "id": str(hit.get("id")),
            "url": rendition.get("url"),
            "width": int(rendition.get("width") or 0),
            "height": int(rendition.get("height") or 0),
            "duration": float(hit.get("duration") or 0),
            "metadata": " ".join([
                str(hit.get("tags", "")),
                str(hit.get("pageURL", ""))
            ]),
            "tags": str(hit.get("tags", "")),
            "creator": hit.get("user", "Unknown"),
            "page_url": hit.get("pageURL", ""),
            "license": "Pixabay Content License",
        })

    return results


def pexels_search(query):
    if not PEXELS_KEY:
        return []

    response = _footage_source_get(
        "Pexels",
        "https://api.pexels.com/v1/videos/search",
        headers={
            "Authorization": PEXELS_KEY
        },
        params={
            "query": query,
            "per_page": 20,
            "size": "medium"
        },
        timeout=35
    )

    if response.status_code == 429:
        raise RuntimeError(
            "Pexels API rate limit reached."
        )

    response.raise_for_status()

    results = []

    for video in response.json().get("videos", []):
        files = sorted(
            video.get("video_files", []),
            key=lambda x: (
                0 if is_native_portrait_916(x.get("width") or 0, x.get("height") or 0) else 1,
                0 if x.get("quality") == "hd" else 1,
                -int(x.get("height") or 0),
            )
        )

        files = [
            f for f in files
            if f.get("link")
            and int(f.get("width") or 0) >= MIN_CLIP_WIDTH
        ]

        if not files:
            continue

        file = files[0]

        slug = re.sub(
            r"[-_/]+",
            " ",
            str(video.get("url", ""))
        )

        results.append({
            "source": "Pexels",
            "id": str(video.get("id")),
            "url": file["link"],
            "width": int(file.get("width") or 0),
            "height": int(file.get("height") or 0),
            "duration": float(video.get("duration") or 0),
            "metadata": slug,
            "tags": slug,
            "creator": (
                video.get("user") or {}
            ).get("name", "Unknown"),
            "page_url": video.get("url", ""),
            "license": "Pexels License",
        })

    return results


def nasa_search(query):
    """NASA public media API; video assets only."""
    response = _footage_source_get(
        "NASA",
        "https://images-api.nasa.gov/search",
        params={
            "q": query,
            "media_type": "video",
            "page_size": 16
        },
        timeout=35
    )

    response.raise_for_status()

    results = []

    for item in response.json().get(
        "collection", {}
    ).get("items", [])[:6]:

        data = (item.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id")

        if not nasa_id:
            continue

        metadata = " ".join([
            str(data.get("title", "")),
            str(data.get("description", "")),
            " ".join(data.get("keywords") or [])
        ])

        try:
            assets = _get_nasa_asset_manifest(nasa_id)
        except (FootageSourceCooldownActive, FootageSourceBudgetExceeded) as exc:
            exc.partial_results = list(results)
            raise
        except Exception as exc:
            if (
                getattr(getattr(exc, "response", None), "status_code", None) == 429
                or footage_source_cooldown_remaining("NASA") > 0
            ):
                try:
                    exc.partial_results = list(results)
                except Exception:
                    pass
                raise
            print(f"NASA asset lookup skipped ({nasa_id}): {exc}")
            continue

        urls = [
            str(x.get("href", ""))
            for x in assets
            if str(x.get("href", "")).split(
                "?", 1
            )[0].lower().endswith(
                (".mp4", ".mov", ".m4v")
            )
        ]

        if not urls:
            continue

        urls.sort(
            key=lambda u: (
                0 if re.search(
                    r"(medium|small|low)",
                    u,
                    re.I
                ) else 1,
                len(u)
            )
        )

        results.append({
            "source": "NASA",
            "id": str(nasa_id),
            "title": str(data.get("title", "")),
            "url": urls[0],
            # NASA search does not consistently expose video dimensions;
            # the downloaded asset is checked with ffprobe before vision/API use.
            "width": 0,
            "height": 0,
            "duration": 0,
            "metadata": metadata,
            "tags": metadata,
            "creator": data.get("center", "NASA"),
            "page_url": item.get(
                "href",
                f"https://images.nasa.gov/details/{nasa_id}"
            ),
            "license": (
                "NASA media usage guidelines; "
                "verify third-party credits where noted"
            ),
        })

    return results


def commons_search(query):
    """Search Commons with subject-preserving query relaxation.

    Commons search ANDs every term, so a natural-language scene description such
    as "ant colony underground tunnels macro" matches no file at all. The full
    query is tried first, then progressively shorter variants that still name the
    scene subject. Relaxed discovery is only discovery: every candidate is still
    filtered by the metadata anchors and reviewed by the visual model.
    """
    variants = footage_queries.provider_query_variants(query, 3) or [clean_query(query)]
    first_error = None
    for index, variant in enumerate(variants):
        try:
            results = _commons_search_once(variant)
        except (FootageSourceBudgetExceeded, FootageSourceCooldownActive):
            raise
        except Exception as exc:
            # A provider hiccup on a relaxed variant must not hide results the
            # strict query could still deliver.
            if first_error is None:
                first_error = exc
            print(f"  Wikimedia Commons: query {variant!r} failed: {str(exc)[:200]}")
            continue
        if results:
            if index:
                print(
                    f"  Wikimedia Commons: relaxed query {variant!r} produced "
                    f"{len(results)} candidate(s)."
                )
            return results
    if first_error is not None and len(variants) == 1:
        raise first_error
    return []


def _commons_search_once(query):
    """Make one paced Commons search; the caller cools the source after HTTP 429."""
    global WIKIMEDIA_LAST_REQUEST_AT

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:video {clean_query(query)}",
        "gsrnamespace": 6,
        "gsrlimit": 20,
        # videoinfo carries the play length, and categories are the English
        # subject labels many non-English uploads rely on.
        "prop": "videoinfo|categories",
        "viprop": "url|size|mime|extmetadata|metadata",
        "clshow": "!hidden",
        "cllimit": 20,
    }
    headers = {
        "User-Agent": WIKIMEDIA_USER_AGENT
    }

    response = _footage_source_get(
        "Wikimedia Commons",
        "https://commons.wikimedia.org/w/api.php",
        params=params,
        headers=headers,
        timeout=35,
    )
    response.raise_for_status()

    pages = response.json().get("query", {}).get("pages", {})
    results = []

    for page in pages.values():
        info = (page.get("videoinfo") or page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime", ""))
        if not mime.startswith("video/"):
            continue

        ext = info.get("extmetadata") or {}
        license_name = re.sub(
            r"<[^>]+>", "",
            str((ext.get("LicenseShortName") or {}).get("value", "")),
        )
        license_normalized = (
            license_name.lower().replace(" ", "").replace("_", "-")
        )
        restricted = any(
            term in license_normalized
            for term in (
                "noncommercial", "non-commercial", "cc-by-nc", "ccby-nc",
                "noderivatives", "no-derivatives", "cc-by-nd", "ccby-nd",
            )
        )
        reusable = (
            "cc0" in license_normalized
            or "publicdomain" in license_normalized
            or "public-domain" in license_normalized
            or "ccby" in license_normalized
            or "creativecommonsattribution" in license_normalized
        )
        if restricted or not reusable:
            continue

        url = info.get("url", "")
        if not url:
            continue

        artist = re.sub(
            r"<[^>]+>", "",
            str((ext.get("Artist") or {}).get("value", "Unknown")),
        )
        description = re.sub(
            r"<[^>]+>", " ",
            str((ext.get("ImageDescription") or {}).get("value", "")),
        )
        categories = " ".join(
            re.sub(r"^Category:", "", str(category.get("title") or ""))
            for category in (page.get("categories") or [])
            if isinstance(category, dict)
        )
        duration = 0.0
        for entry in info.get("metadata") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name") or "").lower() not in (
                "length", "playtime_seconds", "duration"
            ):
                continue
            raw_length = str(entry.get("value") or "").strip()
            try:
                duration = float(raw_length)
            except (TypeError, ValueError):
                try:
                    parts = [float(part) for part in raw_length.split(":")]
                    duration = 0.0
                    for part in parts:
                        duration = duration * 60 + part
                except (TypeError, ValueError):
                    duration = 0.0
            break

        metadata = f"{page.get('title', '')} {description} {categories}"
        results.append({
            "source": "Wikimedia Commons",
            "id": str(page.get("pageid")),
            "title": str(page.get("title", "")),
            "url": url,
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
            "duration": duration,
            "metadata": metadata,
            "tags": f"{page.get('title', '')} {categories}",
            "creator": artist,
            "page_url": (
                page.get("canonicalurl")
                or "https://commons.wikimedia.org/wiki/"
                + str(page.get("title", "")).replace(" ", "_")
            ),
            "license": license_name or "See file page",
        })

    return results


def internet_archive_search(query):
    """Search Internet Archive with subject-preserving query relaxation.

    Its advanced search treats an over-specific query as a set of required
    terms; the strict query runs first and shorter variants are only tried when
    it finds nothing. Licences are still checked per item below.
    """
    variants = footage_queries.provider_query_variants(query, 3) or [clean_query(query)]
    for index, variant in enumerate(variants):
        results = _internet_archive_search_once(variant)
        if results:
            if index:
                print(
                    f"  Internet Archive: relaxed query {variant!r} produced "
                    f"{len(results)} candidate(s)."
                )
            return results
    return []


def _internet_archive_search_once(query):
    """Search Internet Archive for video files with an explicit reusable license."""
    query = clean_query(query)
    terms = [term for term in query.lower().split() if len(term) > 2]
    if not terms:
        return []

    # Query title, description and subject separately so a single rare phrase
    # is not required to match the item's full metadata.
    clauses = []
    for term in terms[:7]:
        safe_term = re.sub(r"[^a-z0-9]", "", term)
        if not safe_term:
            continue
        clauses.extend([
            f'title:"{safe_term}"',
            f'description:"{safe_term}"',
            f'subject:"{safe_term}"',
        ])

    if not clauses:
        return []

    search_query = "mediatype:movies AND (" + " OR ".join(clauses) + ")"

    try:
        response = _footage_source_get(
            "Internet Archive",
            "https://archive.org/advancedsearch.php",
            params=[
                ("q", search_query),
                ("fl[]", "identifier"),
                ("fl[]", "title"),
                ("fl[]", "description"),
                ("fl[]", "creator"),
                ("fl[]", "subject"),
                ("fl[]", "licenseurl"),
                ("fl[]", "rights"),
                ("fl[]", "source_pixel_width"),
                ("fl[]", "runtime"),
                ("rows", "30"),
                ("page", "1"),
                ("output", "json"),
            ],
            headers={
                "User-Agent": "AutoDocumentaryVideo/3.0 (footage research)"
            },
            timeout=40,
        )
        response.raise_for_status()
        documents = response.json().get("response", {}).get("docs", [])
        if not documents:
            print(f"  Internet Archive: no item matched {query!r}.")
    except requests.RequestException as exc:
        if (
            getattr(getattr(exc, "response", None), "status_code", None) == 429
            or footage_source_cooldown_remaining("Internet Archive") > 0
        ):
            raise
        print(f"  Internet Archive search failed: {exc}")
        return []
    except (ValueError, KeyError) as exc:
        print(f"  Internet Archive search returned invalid data: {exc}")
        return []

    def classify_license(values):
        raw = " ".join(str(value or "") for value in values).lower()
        compact = re.sub(r"[\s_-]+", "", raw)

        restricted = any(term in compact for term in (
            "bync", "noncommercial", "bynd", "noderivatives",
            "bysa", "sharealike"
        ))
        if restricted:
            return None

        if (
            "creativecommons.org/publicdomain/zero" in raw
            or re.search(r"\bcc0\b", raw)
        ):
            return "CC0"

        if (
            "creativecommons.org/licenses/by/" in raw
            or re.search(r"\bcc[- ]by(?:\s|$)", raw)
        ):
            return "CC BY"

        if (
            "creativecommons.org/publicdomain/mark/" in raw
            or re.search(r"\bpublic domain\b", raw)
        ):
            return "Public Domain"

        return None

    def parse_runtime(value):
        if value in (None, ""):
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            parts = str(value).strip().split(":")
            try:
                seconds = 0.0
                for part in parts:
                    seconds = seconds * 60 + float(part)
                return seconds
            except (TypeError, ValueError):
                return 0.0

    results = []
    metadata_checks = 0
    rejected_license = 0
    for item in documents[:16]:
        if len(results) >= 8 or metadata_checks >= 12:
            break

        identifier = str(item.get("identifier") or "").strip()
        if not identifier:
            continue

        # Search results expose licenseurl. Reject unknown or restrictive
        # declarations before making the per-item metadata request.
        license_name = classify_license([
            item.get("licenseurl"),
            item.get("rights"),
        ])
        if not license_name:
            rejected_license += 1
            continue

        metadata_cache = CACHE / (
            "internet-archive-"
            + hashlib.sha256(identifier.encode()).hexdigest()
            + ".json"
        )
        archive_item = None

        if (
            metadata_cache.exists()
            and time.time() - metadata_cache.stat().st_mtime < 7 * 86400
        ):
            try:
                archive_item = json.loads(
                    metadata_cache.read_text(encoding="utf-8")
                )
            except Exception:
                archive_item = None

        if archive_item is None:
            metadata_checks += 1
            try:
                metadata_response = _footage_source_get(
                    "Internet Archive",
                    f"https://archive.org/metadata/{identifier}",
                    headers={
                        "User-Agent": "AutoDocumentaryVideo/3.0 (footage research)"
                    },
                    timeout=35,
                )
                metadata_response.raise_for_status()
                archive_item = metadata_response.json()
                try:
                    metadata_cache.write_text(
                        json.dumps(archive_item),
                        encoding="utf-8"
                    )
                except Exception:
                    pass
            except (FootageSourceCooldownActive, FootageSourceBudgetExceeded) as exc:
                exc.partial_results = list(results)
                raise
            except requests.RequestException as exc:
                if (
                    getattr(getattr(exc, "response", None), "status_code", None) == 429
                    or footage_source_cooldown_remaining("Internet Archive") > 0
                ):
                    exc.partial_results = list(results)
                    raise
                print(f"  Internet Archive metadata skipped ({identifier}): {exc}")
                continue
            except ValueError as exc:
                print(f"  Internet Archive metadata skipped ({identifier}): {exc}")
                continue

        metadata = archive_item.get("metadata") or {}
        license_name = classify_license([
            item.get("licenseurl"),
            item.get("rights"),
            metadata.get("licenseurl"),
            metadata.get("rights"),
        ])
        if not license_name:
            continue

        video_files = []
        for archive_file in archive_item.get("files", []):
            filename = str(archive_file.get("name") or "")
            if not filename.lower().endswith((".mp4", ".m4v", ".mov")):
                continue
            if archive_file.get("private") in ("true", True, 1):
                continue
            try:
                file_size = int(archive_file.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if file_size <= 50000 or file_size > MAX_EXCEPTIONAL_CLIP_BYTES:
                continue
            video_files.append((archive_file, file_size))

        if not video_files:
            continue

        def file_preference(entry):
            archive_file, file_size = entry
            filename = str(archive_file.get("name") or "").lower()
            width_value = archive_file.get("width") or item.get("source_pixel_width") or 0
            try:
                width = int(float(width_value))
            except (TypeError, ValueError):
                width = 0
            try:
                height = int(float(archive_file.get("height") or 0))
            except (TypeError, ValueError):
                height = 0
            return (
                0 if is_native_portrait_916(width, height) else 1,
                0 if filename.endswith(".mp4") else 1,
                0 if width >= MIN_CLIP_WIDTH else 1,
                abs(width - 1080) if width else 99999,
                file_size,
            )

        video_files.sort(key=file_preference)
        selected_file = video_files[0][0]
        filename = str(selected_file.get("name") or "")
        if not filename:
            continue

        title = str(metadata.get("title") or item.get("title") or identifier)
        description = metadata.get("description") or item.get("description") or ""
        if isinstance(description, list):
            description = " ".join(str(value) for value in description)

        creator = metadata.get("creator") or item.get("creator") or "Unknown"
        if isinstance(creator, list):
            creator = ", ".join(str(value) for value in creator)

        subjects = metadata.get("subject") or item.get("subject") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        subject_text = " ".join(str(value) for value in subjects)
        combined_metadata = " ".join((title, str(description), subject_text, str(creator)))

        width_value = (
            selected_file.get("width")
            or item.get("source_pixel_width")
            or 0
        )
        try:
            width = int(float(width_value))
        except (TypeError, ValueError):
            width = 0

        file_url = (
            "https://archive.org/download/"
            + quote(identifier, safe="")
            + "/"
            + quote(filename, safe="")
        )
        page_url = "https://archive.org/details/" + quote(identifier, safe="")

        results.append({
            "source": "Internet Archive",
            "id": identifier,
            "title": title,
            "url": file_url,
            "width": width,
            "height": int(selected_file.get("height") or 0),
            "file_size": file_size,
            "duration": parse_runtime(item.get("runtime") or metadata.get("runtime")),
            "metadata": combined_metadata,
            "tags": subject_text or combined_metadata,
            "creator": str(creator),
            "page_url": page_url,
            "license": license_name,
        })

    if not results:
        print(
            f"  Internet Archive: {len(documents)} item(s) matched {query!r} but "
            f"none had a reusable licence and a usable video file "
            f"({rejected_license} rejected for licence)."
        )
    return results

def _deduplicate_footage_results(items):
    deduplicated = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = str(item.get("url") or "").strip().lower()
        if not identity:
            identity = f"{item.get('source', '')}:{item.get('id', '')}".lower()
        if not identity or identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(item)
    return deduplicated



def coverr_search(query):
    """Search Coverr for free, authentic native-vertical stock clips.

    Coverr's API supports is_vertical, is_premium, and is_ai_generated filters.
    We request only non-premium, non-AI vertical results, then require the exact
    9:16 ratio from catalog metadata when available and always verify the file
    itself with ffprobe after download.
    """
    if not COVERR_API_KEY:
        return []

    query = clean_query(query)
    if not query:
        return []

    response = _footage_source_get(
        "Coverr",
        "https://api.coverr.co/videos",
        params={
            "query": query,
            "page": 0,
            "page_size": 20,
            "sort": "popular",
            "filter": "is_vertical:true,is_premium:false,is_ai_generated:false",
            "urls": "true",
        },
        headers={
            "x-api-key": COVERR_API_KEY,
            "Accept": "application/json",
            "User-Agent": "AutoDocumentaryVideo/4.0 (+https://github.com/nikmatpertama-sys/auto-documentary-video)",
        },
        timeout=30,
    )

    try:
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            print(
                "  Coverr API authentication/permission failed "
                f"(HTTP {status}); check the COVERR_API_KEY secret and API access."
            )
            return []
        raise
    except (ValueError, TypeError) as exc:
        print(f"  Coverr API returned invalid JSON: {exc}")
        return []

    if isinstance(payload, dict):
        hits = payload.get("hits", payload.get("videos", []))
        if isinstance(hits, dict):
            hits = hits.get("hits", [])
    else:
        hits = []

    if not isinstance(hits, list):
        return []

    def parse_ratio(value):
        text_value = str(value or "").strip().lower()
        if not text_value:
            return None
        if ":" in text_value:
            left, right = text_value.split(":", 1)
            try:
                return float(left) / float(right)
            except (TypeError, ValueError, ZeroDivisionError):
                return None
        try:
            return float(text_value)
        except (TypeError, ValueError):
            return None

    def safe_coverr_media_url(value):
        value = str(value or "").strip()
        if not value:
            return ""
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host == "coverr.co" or host.endswith(".coverr.co")
        ):
            return ""
        return value

    results = []
    for video in hits[:20]:
        if not isinstance(video, dict):
            continue
        if video.get("is_vertical") is not True:
            continue
        if video.get("is_premium") is True:
            continue
        if video.get("is_ai_generated") is True:
            continue

        ratio = parse_ratio(video.get("aspect_ratio"))
        if ratio is not None and abs(ratio - TARGET_SOURCE_ASPECT) > SOURCE_ASPECT_TOLERANCE:
            # is_vertical also includes ratios such as 4:5 and 2:3. The generator
            # requires real 9:16 and must not silently crop a different format.
            continue

        urls = video.get("urls") or {}
        if not isinstance(urls, dict):
            urls = {}
        media_url = safe_coverr_media_url(
            urls.get("mp4_download") or urls.get("mp4")
        )
        if not media_url:
            continue

        video_id = str(video.get("id") or video.get("slug") or "").strip()
        if not video_id:
            continue

        title = str(video.get("title") or video.get("name") or video_id).strip()
        description = str(video.get("description") or "").strip()
        raw_tags = video.get("tags") or []
        if isinstance(raw_tags, str):
            tags = raw_tags
        elif isinstance(raw_tags, list):
            tags = " ".join(
                str(tag.get("name") or tag.get("title") or "")
                if isinstance(tag, dict) else str(tag)
                for tag in raw_tags
            )
        else:
            tags = ""

        creator_value = video.get("creator") or video.get("author") or {}
        if isinstance(creator_value, dict):
            creator = str(
                creator_value.get("name")
                or creator_value.get("display_name")
                or creator_value.get("username")
                or "Coverr creator"
            )
            creator_url = str(
                creator_value.get("profile_url")
                or creator_value.get("url")
                or ""
            )
        else:
            creator = str(creator_value or "Coverr creator")
            creator_url = str(video.get("creator_url") or "")

        page_url = str(video.get("page_url") or video.get("url") or "").strip()
        if not page_url.startswith("https://coverr.co/"):
            page_url = "https://coverr.co/videos/" + quote(
                str(video.get("slug") or video_id), safe=""
            )

        def positive_dimension(*values):
            for value in values:
                try:
                    parsed = int(float(value or 0))
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed
            return 0

        width = positive_dimension(video.get("width"), video.get("max_width"))
        height = positive_dimension(video.get("height"), video.get("max_height"))

        try:
            duration = float(video.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0

        metadata = " ".join(
            part for part in (
                title,
                description,
                tags,
                "native vertical portrait stock video",
                str(video.get("scene") or ""),
            ) if part
        )

        results.append({
            "source": "Coverr",
            "id": video_id,
            "title": title,
            "url": media_url,
            "width": width,
            "height": height,
            "duration": duration,
            "metadata": metadata,
            "tags": tags or metadata,
            "creator": creator,
            "creator_url": creator_url,
            "page_url": page_url,
            "license": (
                "Coverr stock library; commercial use permitted under Coverr License "
                "(updated 2026-11-04); free Demo API attribution required"
            ),
            "is_vertical": True,
            "is_ai_generated": False,
            "aspect_ratio": video.get("aspect_ratio") or "9:16",
        })

    return results


def search_all_footage_sources(query, include_coverr=False):
    """Search sources independently so one 429 cannot discard successful providers."""
    searches = [
        ("Internet Archive", internet_archive_search),
        ("Wikimedia Commons", commons_search),
        ("NASA", nasa_search),
        ("Web Search", web_page_video_search),
        ("Google Search", google_open_web_video_search),
    ]
    # Coverr is queried only for the primary scene query and one targeted retry.
    # Its Demo API has a finite request/hour allowance; this avoids spending
    # requests on every synonym generated by the general search expansion.
    if include_coverr and ENABLE_COVERR_SOURCE:
        searches.insert(0, ("Coverr", coverr_search))
    if ENABLE_PEXELS_FALLBACK and PEXELS_KEY:
        searches.append(("Pexels", pexels_search))
    if ENABLE_PIXABAY_FALLBACK and PIXABAY_KEY:
        searches.append(("Pixabay", pixabay_search))

    combined = []
    for source, search_fn in searches:
        cached = _read_footage_source_cache(source, query, allow_stale=False)
        if cached is not None:
            print(
                f"  {source}: {len(cached['results'])} cached candidate(s) "
                f"(independent source cache, age {cached['age'] / 60:.0f}m)."
            )
            combined.extend(cached["results"])
            continue

        stale_cache = _read_footage_source_cache(source, query, allow_stale=True)
        cooldown = footage_source_cooldown_remaining(source)
        if cooldown > 0:
            if stale_cache is not None:
                print(
                    f"  {source}: serving {len(stale_cache['results'])} stale cached candidate(s) "
                    f"while source cools down ({cooldown:.0f}s remaining)."
                )
                combined.extend(stale_cache["results"])
            else:
                print(
                    f"  {source}: skipped during persisted cooldown "
                    f"({cooldown:.0f}s remaining); no stale cache available."
                )
            continue

        FOOTAGE_SOURCE_DISABLED.discard(source)
        try:
            found = search_fn(query)
            if not isinstance(found, list):
                found = []
            combined.extend(found)
            if footage_source_cooldown_remaining(source) > 0:
                # A nested metadata/manifest request failed. Keep the partial
                # candidates for this run, but never cache them as a complete search.
                print(f"  {source}: source entered cooldown mid-search; partial results were not cached.")
                if stale_cache is not None:
                    combined.extend(stale_cache["results"])
            else:
                # Cache successful empty results too: repeated no-hit queries should
                # not hammer the same catalog for the full TTL.
                _write_footage_source_cache(source, query, found)
                print(f"  {source}: {len(found)} candidate(s); source result cached.")
        except (FootageSourceBudgetExceeded, FootageSourceCooldownActive) as exc:
            partial = getattr(exc, "partial_results", [])
            if isinstance(partial, list):
                combined.extend(partial)
            if stale_cache is not None:
                print(
                    f"  {source}: using stale cache after source governor skip "
                    f"({len(stale_cache['results'])} candidate(s))."
                )
                combined.extend(stale_cache["results"])
            else:
                print(f"  {source}: {exc}")
        except Exception as exc:
            partial = getattr(exc, "partial_results", [])
            if isinstance(partial, list):
                combined.extend(partial)
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                # The request wrapper normally persists this before the error
                # reaches here; repeat safely for custom provider call paths.
                mark_footage_source_rate_limited(source, response)
            elif status_code in (408, 425):
                mark_footage_source_temporarily_unavailable(source, 45.0)
            elif status_code is not None and 500 <= int(status_code) <= 599:
                mark_footage_source_temporarily_unavailable(source, 90.0)
            elif isinstance(exc, requests.RequestException):
                mark_footage_source_temporarily_unavailable(source, 60.0)
            print(f"  {source} search unavailable for '{query}': {exc}")
            if stale_cache is not None:
                print(
                    f"  {source}: using stale cache after failed refresh "
                    f"({len(stale_cache['results'])} candidate(s))."
                )
                combined.extend(stale_cache["results"])

    deduplicated = _deduplicate_footage_results(combined)
    if len(deduplicated) != len(combined):
        print(f"  Source deduplication: {len(combined) - len(deduplicated)} duplicate candidate(s) removed.")
    return deduplicated


STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "over", "of",
    "under", "through", "about", "near", "video",
    "cinematic", "footage", "close", "up", "wide", "angle",
    "slow", "motion", "beautiful", "nature", "scene", "view", "real",
    "world", "camera", "showing", "moving", "during", "while", "where",
    "what", "when", "which", "their", "there", "they", "these", "those",
    "onto", "its", "are", "was", "were", "been", "being", "has",
    "have", "had", "will", "would", "could", "should", "can", "may",
    "one", "two", "first", "second", "then", "than", "also", "more",
    "most", "very", "some", "many", "such", "like", "make", "made",
    "high", "low", "level", "shot", "shots", "footage", "clip"
}

# Synonym groups bridge the gap between a scene's precise wording and the
# shorter tags commonly supplied by stock-footage libraries.
CONCEPT_GROUPS = {
    "rainforest": {
        "amazon", "rainforest", "jungle",
        "tropical", "tropics", "equatorial"
    },
    "canopy": {
        "canopy", "treetop", "treetops",
        "tree", "trees", "foliage", "crown",
        "leaf", "leaves"
    },
    "sunrise": {
        "sunrise", "dawn", "daybreak", "morning",
        "sunlight", "sun", "golden"
    },
    "forest": {
        "forest", "woodland", "woods", "timberland", "trees"
    },
    "aerial": {
        "aerial", "drone", "overhead",
        "topdown", "birdseye", "helicopter", "fromabove"
    },
    "river": {
        "river", "rivers", "waterway", "tributary", "stream"
    },
    "waterfall": {
        "waterfall", "falls", "cascade", "cascades"
    },
    "wildlife": {
        "wildlife", "animal", "animals", "fauna"
    },
    "insect": {
        "insect", "insects", "bug", "bugs", "ant", "ants",
        "beetle", "beetles", "butterfly", "butterflies",
        "mosquito", "mosquitoes", "dragonfly", "dragonflies"
    },
    "orchid": {
        "orchid", "orchids", "orchidaceae"
    },
    "jaguar": {
        "jaguar", "jaguars"
    },
    "rain": {
        "rain", "rainstorm", "rainstorms", "rainfall",
        "raining", "downpour", "monsoon", "raindrop", "raindrops"
    },
    "forest_floor": {
        "floor", "undergrowth", "understory",
        "understorey", "leaflitter", "groundcover"
    },
    "bird": {
        "bird", "birds", "avian", "parrot",
        "parrots", "macaw", "macaws"
    },
    "mountain": {
        "mountain", "mountains", "mountainous",
        "peak", "peaks", "alpine"
    },
    "ocean": {
        "ocean", "sea", "marine", "underwater"
    },
    "desert": {
        "desert", "arid", "dunes", "sand"
    },
    # 10.11: distinguish field research and measuring equipment from generic
    # NASA/space-program metadata that can coincidentally mention a crew/team.
    "research_activity": {
        "researcher", "researchers", "scientist", "scientists",
        "research", "researching", "fieldwork", "field", "fieldstudy",
        "investigator", "technician", "biologist", "ecologist",
        "team", "crew", "expedition", "sampling", "sample",
    },
    "field_instrument": {
        "sensor", "sensors", "humidity", "humidimeter", "hygrometer",
        "thermometer", "barometer", "anemometer", "weather", "station",
        "weatherstation", "meteorological", "temperature", "measurement",
        "measuring", "instrument", "instruments", "equipment", "datalogger",
        "monitoring", "monitor", "logger",
    },
}


CONTEXT_CONFLICTS = {
    "snow_winter": {
        "snow", "snowy", "winter", "ice",
        "icy", "frost", "frozen"
    },
    "autumn": {
        "autumn", "fall", "falling", "maple", "orangeleaves"
    },
    "underwater": {
        "underwater", "submarine", "coral",
        "reef", "fish", "fishes"
    },
    "urban": {
        "urban", "city", "cities", "street",
        "building", "buildings", "traffic"
    },
    "desert": {
        "desert", "dunes", "arid", "sandstorm"
    },
    "spaceflight": {
        "shuttle", "spacecraft", "astronaut", "astronauts", "rocket",
        "launchpad", "liftoff", "orbiter", "kennedy", "columbiaaccident",
    },
    "military": {
        "military", "soldier", "soldiers", "army", "troop", "troops",
        "combat", "battle", "warfare", "tactical", "weapon", "weapons",
        "rifle", "firearm", "bootcamp", "drill", "barracks",
    },
    "industrial": {
        "industrial", "industry", "factory", "factories", "refinery",
        "manufacturing", "warehouse", "workshop", "assemblyline",
    },
    "agriculture": {
        "agriculture", "agricultural", "farmland", "farm", "farms",
        "farmer", "farmers", "tractor", "crop", "crops", "livestock",
        "pasture", "plantation",
    },
    "sports": {
        "sport", "sports", "athlete", "athletes", "stadium", "match",
        "soccer", "football", "basketball", "tennis", "racing", "racecar",
    },
    "volcanic_ash": {
        "volcanic", "volcano", "volcanoes", "eruption", "eruptions",
        "ash", "sulfur", "sulphur", "sulfurdioxide",
    },
}


def query_concepts(text):
    tokens = set(
        normalize_words(text)
    ) - STOP_WORDS

    concepts = {}

    for name, aliases in CONCEPT_GROUPS.items():
        if tokens & aliases:
            concepts[name] = aliases

    covered = (
        set().union(*concepts.values())
        if concepts
        else set()
    )

    for token in tokens - covered:
        if len(token) > 2:
            concepts[f"term:{token}"] = {token}

    return tokens, concepts


def expand_visual_queries(raw_queries, narration=""):
    """Build deduplicated, scene-aware footage searches without repeating words."""
    expanded = []
    seen = set()

    replacements = (
        (r"\bAmazon\b", "tropical rainforest"),
        (r"\bcanopy\b", "treetops"),
        (r"\bsunrise\b", "dawn"),
        (r"\baerial view\b", "drone view"),
    )

    mood_words = {
        "sunrise", "sunset", "dawn", "dusk",
        "morning", "evening", "golden",
        "cinematic", "slow", "motion"
    }

    def add_query(value):
        cleaned = clean_query(value)
        if not cleaned:
            return

        # De-duplicate tokens such as the previous "amazon canopy canopy" query.
        deduplicated = []
        token_seen = set()
        for token in normalize_words(cleaned):
            if token not in token_seen:
                token_seen.add(token)
                deduplicated.append(token)

        normalized = " ".join(deduplicated).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            expanded.append(normalized)
            seen.add(key)

    # Every footage API ANDs the terms of a query, so a long natural-language
    # scene description frequently returns zero hits. Short subject-preserving
    # layers go first because they are the queries that actually reach footage;
    # each one still names the scene subject and is filtered as strictly below.
    for raw in raw_queries:
        for layer in footage_queries.layered_queries(raw, narration, limit=4):
            add_query(layer)

    for raw in raw_queries:
        base = clean_query(raw)
        if not base:
            continue

        add_query(base)

        variant = base
        for pattern, replacement in replacements:
            variant = re.sub(
                pattern,
                replacement,
                variant,
                flags=re.IGNORECASE
            )
        add_query(variant)

        core_words = [
            token
            for token in normalize_words(base)
            if token not in STOP_WORDS and token not in mood_words
        ]
        add_query(" ".join(core_words))

    joined = " ".join(expanded).lower()
    context_text = (joined + " " + str(narration or "")).lower()
    context_tokens = set(normalize_words(context_text))

    if "amazon" in joined and ("canopy" in joined or "treetop" in joined):
        for query in (
            "Amazon rainforest aerial canopy",
            "Amazon rainforest drone canopy",
            "Brazil Amazon rainforest aerial treetops",
            "tropical rainforest canopy drone view",
            "Amazon jungle overhead forest canopy",
        ):
            add_query(query)

    has_research_context = bool(context_tokens & {
        "researcher", "researchers", "scientist", "scientists", "research",
        "fieldwork", "field", "study", "studying", "measurement", "measuring",
    })
    has_forest_context = bool(context_tokens & {
        "forest", "forests", "rainforest", "amazon", "jungle", "woodland",
    })
    has_climate_model_context = (
        "climate" in context_tokens
        and bool(context_tokens & {
            "model", "models", "modeling", "modelling", "simulation",
            "computer", "computers", "projection", "projections", "data",
        })
    )

    # Make the most useful fieldwork/data alternatives part of the first search
    # wave. Relying only on the later sparse-pool fallback missed scenes whose
    # raw metadata count was high but whose candidates were semantically weak.
    if has_research_context and has_forest_context:
        for query in (
            "field researcher measuring humidity in tropical forest",
            "forest weather station environmental sensors",
        ):
            add_query(query)

    if has_climate_model_context:
        for query in (
            "climate model simulation visualization earth atmosphere",
            "NASA atmospheric climate model data visualization",
            "scientist analyzing climate model data computer screen",
        ):
            add_query(query)

    # Bound the API cost per scene. Six variants per scene stay inside the
    # per-run request budgets of every provider, including Commons.
    return expanded[:6]


def scene_subject_phrase(query, max_words=2):
    """The scene's subject words, without shot and style descriptors."""
    phrases = footage_queries.subject_phrases(str(query or "")) or []
    if phrases:
        return phrases[0]
    words = [
        word for word in dict.fromkeys(normalize_words(clean_query(str(query or ""))))
        if word not in footage_queries.SHOT_WORDS and len(word) > 2
    ]
    return " ".join(words[:max_words])


def fallback_visual_queries(raw_queries, narration=""):
    """Broaden sparse searches with independent, subject-preserving variants.

    Version 10.13 adds safe hook layout, sentence-bounded captions, strict visual anchors,
    and native 9:16 source validation. Version 10.12 expands sparse-tier search and
    climate-model shot recovery.
    Version 10.11 added documentary fieldwork: people, research activity, and
    environmental instruments. The variants deliberately separate wide
    shots of researchers from close-ups of sensors so a scene can be covered
    by a truthful, visually verified relevant shot without demanding every
    object appear in the same frame.
    """
    primary = next(
        (str(value).strip() for value in raw_queries if str(value).strip()),
        "",
    )
    if not primary:
        return []

    context_text = " ".join(
        [str(value or "") for value in raw_queries]
        + [str(narration or "")]
    ).lower()
    context_tokens = set(normalize_words(context_text))
    _, groups = query_concepts(context_text)
    names = set(groups)
    subject_phrase = scene_subject_phrase(primary)
    candidates = []

    research_terms = {
        "researcher", "researchers", "scientist", "scientists", "fieldwork",
        "research", "researching", "investigator", "technician", "team",
        "measuring", "measurement", "monitoring", "sample", "sampling",
        "humidity", "sensor", "sensors", "weather", "meteorological",
        "station", "temperature", "climate",
    }
    has_research_context = bool(context_tokens & research_terms)
    has_forest_context = bool(context_tokens & {
        "forest", "rainforest", "amazon", "tropical", "jungle", "woodland"
    })
    has_weather_context = bool(context_tokens & {
        "humidity", "sensor", "sensors", "weather", "meteorological",
        "station", "temperature", "climate", "measurement", "measuring",
    })
    has_climate_model_context = (
        "climate" in context_tokens
        and bool(context_tokens & {
            "model", "models", "modeling", "modelling", "simulation",
            "computer", "computers", "projection", "projections", "data",
        })
    )

    if has_research_context:
        if has_forest_context:
            candidates.extend([
                "scientist conducting field research in tropical rainforest",
                "field researcher measuring humidity in tropical forest",
                "environmental scientist recording forest data",
                "research team conducting rainforest fieldwork",
                "forest weather station sensors and monitoring equipment",
                "climate monitoring instruments in tropical forest",
            ])
        else:
            candidates.extend([
                "scientist conducting outdoor field research",
                "field researcher recording environmental measurements",
                "research team monitoring scientific instruments outdoors",
                "weather station sensors and monitoring equipment",
                "temperature humidity data logger field equipment",
                "meteorological instruments at research station",
            ])

    if has_climate_model_context:
        # A scientific model can be represented truthfully by its data output;
        # the clip need not show a researcher physically operating a computer.
        # These are separate shot options and still pass the same visual check.
        candidates.extend([
            "climate model simulation visualization of earth atmosphere",
            "NASA atmospheric climate model data visualization",
            "scientist analyzing climate model data on computer screen",
            "global temperature climate projection data visualization",
        ])

    if has_weather_context:
        candidates.extend([
            "weather station instruments and sensors outdoors",
            "meteorological station temperature humidity sensors",
            "handheld digital humidity meter field measurement",
        ])

    if "rainforest" in names and "canopy" in names:
        candidates.extend([
            "tropical rainforest canopy aerial",
            "rainforest treetops drone",
            "jungle canopy overhead",
            "tropical forest canopy aerial",
            "jungle treetops aerial",
            "Amazon basin forest canopy",
        ])
    elif "rainforest" in names and "river" in names:
        candidates.extend([
            "tropical rainforest river aerial",
            "jungle river winding",
            "river through tropical forest",
            "Amazon river forest aerial",
        ])
    elif "rainforest" in names:
        candidates.extend([
            "tropical rainforest aerial",
            "jungle forest wildlife",
            "tropical forest trees",
            "rainforest landscape aerial",
        ])
    elif "canopy" in names:
        candidates.extend([
            "forest canopy aerial",
            "tree canopy drone",
            "forest treetops aerial",
        ])
    elif "waterfall" in names:
        candidates.extend([
            "waterfall flowing water",
            "forest waterfall aerial",
            "waterfall nature landscape",
        ])
    elif "river" in names:
        candidates.extend([
            "river flowing aerial",
            "river winding through forest",
            "river landscape drone",
        ])
    elif "insect" in names:
        # The concept group says "insect"; only the scene query knows which one.
        # Dropping that word produced a moth/beetle/grasshopper pool that the
        # visual gate then rejected, so the subject is kept and only the shot
        # wording is broadened.
        candidates.extend([
            f"{subject_phrase} macro nature",
            f"{subject_phrase} on leaf closeup",
            f"{subject_phrase} crawling plant",
        ])
    elif "orchid" in names:
        candidates.extend([
            "orchid flower closeup",
            "orchid tropical plant",
            "orchid growing tree",
        ])
    elif "jaguar" in names:
        candidates.extend([
            "jaguar walking forest",
            "jaguar wildlife habitat",
            "jaguar resting jungle",
        ])
    elif "bird" in names:
        candidates.extend([
            "tropical bird perched tree",
            "bird flying forest",
            "parrot wildlife closeup",
        ])

    unique = []
    seen = set()
    for item in candidates:
        cleaned = clean_query(item)
        key = " ".join(dict.fromkeys(normalize_words(cleaned))).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)

    # Bound the additional API work per difficult scene; every alternative
    # remains independently metadata-filtered and visually verified.
    return unique[:12]


def unrequested_context_conflicts(hit, query, narration=""):
    """Return strong off-topic contexts found in metadata but absent from scene intent."""
    metadata = candidate_search_text(hit)
    metadata_tokens = set(normalize_words(metadata))
    intent_tokens = set(normalize_words(f"{query or ''} {narration or ''}"))
    return tuple(
        name for name, conflict_terms in CONTEXT_CONFLICTS.items()
        if metadata_tokens & conflict_terms and not intent_tokens & conflict_terms
    )


def relevance_score(hit, query, narration):
    """Score explicit title/tags strongly and treat long descriptions as supporting evidence."""
    title_tokens = set(normalize_words(hit.get("title", "")))
    tag_tokens = set(normalize_words(hit.get("tags", "")))
    # Providers expose different fields; treat every descriptive field plus the
    # filename as supporting evidence so missing metadata is not read as off-topic.
    filename_tokens = set(normalize_words(
        unquote(urlparse(str(hit.get("url") or "")).path).replace("_", " ")
    ))
    description_tokens = set(normalize_words(hit.get("metadata", ""))) | filename_tokens
    page_tokens = set(normalize_words(hit.get("page_url", "")))
    primary_metadata_tokens = title_tokens | tag_tokens
    supporting_metadata_tokens = description_tokens | page_tokens
    metadata_tokens = primary_metadata_tokens | supporting_metadata_tokens

    query_tokens, query_groups = query_concepts(query)
    _narration_tokens, narration_groups = query_concepts(narration)

    if not metadata_tokens or not query_tokens:
        return 0.0, 0, max(1, len(query_groups))

    # Core subject and shot-type concepts outweigh mood or generic wording.
    weights = {
        "rainforest": 3.0, "jaguar": 3.2, "insect": 3.0, "orchid": 3.0,
        "wildlife": 2.8, "bird": 2.6, "canopy": 2.7, "aerial": 2.7,
        "river": 2.6, "waterfall": 2.6, "forest_floor": 2.4,
        "ocean": 2.5, "desert": 2.5, "mountain": 2.3,
        "research_activity": 2.4, "field_instrument": 2.4,
    }
    total_weight = sum(
        weights.get(name, 1.0 if name.startswith("term:") else 1.25)
        for name in query_groups
    ) or 1.0

    def group_coverage(tokens):
        covered = sum(
            weights.get(name, 1.0 if name.startswith("term:") else 1.25)
            for name, aliases in query_groups.items()
            if tokens & aliases
        )
        return covered / total_weight

    title_group_coverage = group_coverage(title_tokens)
    tag_group_coverage = group_coverage(tag_tokens)
    supporting_group_coverage = group_coverage(supporting_metadata_tokens)

    # Normalize over fields that actually exist. If a source provides only a
    # long description, it remains discoverable but receives a conservative cap.
    group_parts = []
    if title_tokens:
        group_parts.append((0.42, title_group_coverage))
    if tag_tokens:
        group_parts.append((0.38, tag_group_coverage))
    if supporting_metadata_tokens:
        group_parts.append((0.20, supporting_group_coverage))
    if group_parts:
        part_weight = sum(weight for weight, _coverage in group_parts)
        weighted_group_coverage = sum(
            weight * coverage for weight, coverage in group_parts
        ) / max(0.01, part_weight)
    else:
        weighted_group_coverage = 0.0
    if not primary_metadata_tokens:
        weighted_group_coverage *= 0.45

    direct_coverage = (
        len(query_tokens & primary_metadata_tokens) / max(1, len(query_tokens))
        if primary_metadata_tokens else 0.0
    )
    title_coverage = len(query_tokens & title_tokens) / max(1, len(query_tokens))
    tag_coverage = len(query_tokens & tag_tokens) / max(1, len(query_tokens))
    description_coverage = len(
        query_tokens & supporting_metadata_tokens
    ) / max(1, len(query_tokens))

    narration_matches = sum(
        1 for aliases in narration_groups.values() if metadata_tokens & aliases
    )
    narration_coverage = narration_matches / max(1, min(6, len(narration_groups)))

    camera_aliases = CONCEPT_GROUPS.get("aerial", set())
    camera_requested = bool(query_tokens & camera_aliases)
    camera_match = bool(metadata_tokens & camera_aliases)
    camera_bonus = 0.08 if camera_requested and camera_match else 0.0
    camera_penalty = 0.10 if camera_requested and not camera_match else 0.0

    # Naming the exact subject beats naming its broad category: an ant clip must
    # outrank a generic "insect on a leaf" clip for an ant scene.
    specificity = 0.0
    canonical = footage_queries.canonical_subject(
        footage_queries.content_terms(str(query or ""))
    )
    required = footage_queries.specific_terms(canonical)
    if required:
        # Naming the subject itself is stronger evidence than naming its broad
        # category ("insect", "animal"). The broad-only clip is down-ranked, not
        # discarded: the visual model remains the deciding gate.
        specificity = 0.12 if (metadata_tokens & required) else -0.15

    conflicts = unrequested_context_conflicts(hit, query, narration)
    conflict_penalty = 0.0
    for conflict in conflicts:
        conflict_penalty += 0.32 if conflict in {"military", "sports"} else 0.24

    score = (
        0.42 * weighted_group_coverage
        + 0.16 * direct_coverage
        + 0.14 * title_coverage
        + 0.12 * tag_coverage
        + 0.06 * description_coverage
        + 0.10 * narration_coverage
        + camera_bonus
        + specificity
        - camera_penalty
        - min(0.72, conflict_penalty)
    )

    # The matched count still uses combined metadata for relaxed discovery;
    # admission is never acceptance and still requires the configured visual gate.
    matched_groups = sum(
        1 for aliases in query_groups.values() if metadata_tokens & aliases
    )
    return max(0.0, min(1.0, score)), matched_groups, max(1, len(query_groups))


def required_visual_anchors(query):
    """Extract anchors from the actual search query, not always the primary query."""
    _, groups = query_concepts(str(query or ""))
    return groups


def candidate_matches_visual_anchors(hit, query, primary_query=None):
    """Require metadata to preserve the candidate shot and the scene's core intent.

    Search alternatives may broaden wording, but they must not erase the
    primary requested subject/shot. This prevents a generic "tropical forest"
    fallback from substituting a bird nest for a rainforest canopy aerial.
    """
    anchors = required_visual_anchors(query)
    if not anchors:
        return False

    tokens = set(normalize_words(candidate_search_text(hit)))

    strict_aliases = {
        # Generic forest/tree wording is not enough to claim a rainforest match.
        # Such candidates can still enter the relaxed queue for visual-AI review.
        "rainforest": {
            "amazon", "rainforest", "jungle", "tropical", "tropics", "equatorial",
        },
        "forest": {"forest", "forests", "woodland", "woods", "timberland", "tree", "trees", "rainforest", "jungle"},
        "canopy": {"canopy", "canopies", "treetop", "treetops", "foliage", "crown", "leaf", "leaves", "tree", "trees"},
        "aerial": {"aerial", "drone", "overhead", "topdown", "birdseye", "helicopter", "fromabove"},
        "insect": {"insect", "insects", "bug", "bugs", "ant", "ants", "beetle", "beetles", "butterfly", "butterflies", "mosquito", "mosquitoes", "dragonfly", "dragonflies"},
        "orchid": {"orchid", "orchids", "orchidaceae"},
        "jaguar": {"jaguar", "jaguars"},
        "forest_floor": {"floor", "undergrowth", "understory", "understorey", "leaflitter", "groundcover", "underbrush"},
        "rain": {"rain", "rainstorm", "rainstorms", "rainfall", "raining", "downpour", "monsoon", "raindrop", "raindrops"},
        "river": {"river", "rivers", "waterway", "tributary", "stream"},
        "waterfall": {"waterfall", "falls", "cascade", "cascades"},
        "wildlife": {"wildlife", "animal", "animals", "fauna"},
        "bird": {"bird", "birds", "avian", "parrot", "parrots", "macaw", "macaws"},
        "research_activity": {"researcher", "researchers", "scientist", "scientists", "research", "researching", "fieldwork", "field", "fieldstudy", "investigator", "technician", "biologist", "ecologist", "team", "crew", "expedition", "sampling", "sample"},
        "field_instrument": {"sensor", "sensors", "humidity", "humidimeter", "hygrometer", "thermometer", "barometer", "anemometer", "station", "weatherstation", "meteorological", "temperature", "measurement", "measuring", "instrument", "instruments", "equipment", "datalogger", "logger"},
    }

    def anchor_matches(name, aliases):
        if name.startswith("term:"):
            return name.split(":", 1)[1] in tokens
        return bool(tokens & strict_aliases.get(name, aliases))

    matched = {name for name, aliases in anchors.items() if anchor_matches(name, aliases)}
    subject_groups = {
        "forest", "canopy", "river", "waterfall", "wildlife", "insect",
        "orchid", "jaguar", "bird", "mountain", "ocean", "desert", "forest_floor",
    }
    requested_subjects = subject_groups.intersection(anchors.keys())
    requested_specific = {"insect", "orchid", "jaguar", "bird", "wildlife"}.intersection(anchors.keys())

    if requested_specific and not requested_specific.issubset(matched):
        return False
    for mandatory_anchor in ("research_activity", "field_instrument"):
        if mandatory_anchor in anchors and mandatory_anchor not in matched:
            return False

    # Only the subject the query actually leads with is mandatory. A word that
    # merely appears later in the sentence ("ants carrying leaves" -> "leaves"
    # belongs to the canopy group) describes the shot, not its required subject;
    # demanding every such group would reject correct footage such as a clip of
    # ants carrying seeds. Relevance scoring and the visual model still judge
    # those secondary words.
    head_terms = set(footage_queries.content_terms(str(query or ""))[:2])
    head_groups = {
        name for name, aliases in anchors.items() if head_terms & set(aliases)
    }
    mandatory_subjects = requested_subjects & head_groups
    if mandatory_subjects:
        if not mandatory_subjects.issubset(matched):
            return False
    elif requested_subjects and not (requested_subjects & matched):
        return False

    if "rainforest" in anchors and "rainforest" not in matched:
        return False
    if "aerial" in anchors and "aerial" not in matched:
        return False

    # A broad synonym group is not evidence of a specific subject: "insect on a
    # leaf" is not an ant colony, and "animal" is not a jaguar. When the query
    # names a specific subject, the metadata must name that subject (or a real
    # synonym for it) to qualify strictly. Otherwise it waits for visual review
    # in the partial-metadata queue instead of being accepted.
    canonical = footage_queries.canonical_subject(
        footage_queries.content_terms(str(query or ""))
    )
    required = footage_queries.specific_terms(canonical)
    if required and not (tokens & required):
        return False

    # Preserve core anchors from the original scene query even when the
    # current search is a broad fallback variant. Mood words like sunrise
    # are intentionally excluded.
    if primary_query:
        primary = required_visual_anchors(primary_query)
        primary_core = {
            "rainforest", "forest", "canopy", "aerial", "river", "waterfall",
            "wildlife", "insect", "orchid", "jaguar", "bird", "rain", "ocean",
            "desert", "forest_floor", "mountain",
        }
        # Same rule as above: only the subject the scene query leads with is
        # mandatory. A later word ("...carrying leaves") qualifies the shot and
        # must not veto footage whose subject is correct.
        primary_head = set(footage_queries.content_terms(str(primary_query))[:2])
        for name, aliases in primary.items():
            if name not in primary_core:
                continue
            if not (primary_head & set(aliases)):
                continue
            if not anchor_matches(name, aliases):
                return False

        primary_tokens = set(normalize_words(primary_query))
        exact_intents = {
            "satellite": {"satellite", "satellites", "spaceborne", "remote", "sensing"},
            "cloud": {"cloud", "clouds", "cloudbank", "cloudscape"},
            "plume": {"plume", "plumes", "moisture", "vapor", "vapour"},
            "window": {"window", "windows", "glass", "pane", "panes"},
        }
        for source_term, aliases in exact_intents.items():
            if source_term in primary_tokens and not (tokens & aliases):
                return False

        # "Clear-cut" is a defining ecological condition, not a generic forest shot.
        if "clear" in primary_tokens and "cut" in primary_tokens:
            clearcut_terms = {
                "clearcut", "deforestation", "deforested", "logging", "logged",
                "clearing", "cleared", "clearance", "treecutting", "landclearing",
                "forestloss", "burned", "burnt", "slashburn",
            }
            if not (tokens & clearcut_terms):
                return False

    return bool(matched) or bool(primary_query and required_visual_anchors(primary_query))


def candidate_matches_relaxed_metadata(hit, query, narration="", min_score=0.08):
    """Admit plausible metadata matches to the visual-AI queue.

    This is deliberately not an acceptance decision: every candidate admitted
    only by this relaxed path must still pass downloaded-video visual review.
    """
    score, matched, _total = relevance_score(hit, query, narration)
    return matched >= 1 and score >= float(min_score)


def tag_similarity(tags_a, tags_b):
    a = set(
        normalize_words(tags_a)
    )
    b = set(
        normalize_words(tags_b)
    )

    if not a or not b:
        return 0.0

    return len(a & b) / max(
        1,
        len(a | b)
    )


def _extract_single_frame(video_path, timestamp, frame_path):
    """Write one JPEG frame, tolerating sources whose first seek fails.

    Some archive uploads decode badly on an input-side seek or carry a pixel
    format the MJPEG encoder rejects ("parameters such as bit_rate, rate, width
    or height"). Falling back to an output-side seek with an explicit even-sized
    yuvj420p conversion keeps those clips reviewable instead of losing an
    otherwise valid candidate.
    """
    attempts = (
        [
            "-ss", f"{timestamp:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "5", str(frame_path),
        ],
        [
            "-i", str(video_path), "-ss", f"{timestamp:.3f}",
            "-frames:v", "1",
            "-vf", "scale=768:-2:flags=lanczos,format=yuvj420p",
            "-pix_fmt", "yuvj420p", "-q:v", "5", str(frame_path),
        ],
        [
            "-i", str(video_path), "-frames:v", "1",
            "-vf", "scale=768:-2:flags=lanczos,format=yuvj420p",
            "-pix_fmt", "yuvj420p", "-q:v", "5", str(frame_path),
        ],
    )
    result = None
    for args in attempts:
        result = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0 and frame_path.exists():
            return result
    return result


def extract_vision_frames(video_path, frame_count=3, save_dir=None):
    """Extract evenly spaced, low-resolution frames for visual inspection.

    When ``save_dir`` is given the exact frames shown to the vision provider are
    kept on disk as audit evidence for the accepted clip.
    """
    duration = ffprobe_duration(video_path)

    if duration <= 0:
        raise RuntimeError(
            "Downloaded candidate has no readable video duration."
        )

    fractions = (
        0.20,
        0.50,
        0.80
    )[:max(2, min(3, frame_count))]

    timestamps = []

    for fraction in fractions:
        timestamp = min(
            max(0.1, duration * fraction),
            max(0.1, duration - 0.1)
        )

        if (
            not timestamps
            or abs(timestamp - timestamps[-1]) >= 0.05
        ):
            timestamps.append(timestamp)

    frame_paths = []

    with tempfile.TemporaryDirectory(
        prefix="footage_vision_"
    ) as temp_dir:

        temp_path = Path(temp_dir)

        for index, timestamp in enumerate(timestamps, 1):
            frame_path = temp_path / f"frame_{index:02d}.jpg"

            result = _extract_single_frame(video_path, timestamp, frame_path)

            if (
                result.returncode != 0
                or not frame_path.exists()
            ):
                detail = (
                    result.stderr
                    or "FFmpeg did not create a frame."
                ).strip()

                raise RuntimeError(
                    f"Could not extract visual-check frame: "
                    f"{detail[-700:]}"
                )

            frame_paths.append(
                frame_path.read_bytes()
            )

            if save_dir is not None:
                keep_dir = Path(save_dir)
                keep_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(
                    frame_path,
                    keep_dir / f"frame_{index:02d}_{timestamp:.2f}s.jpg",
                )

    if len(frame_paths) < 2:
        raise RuntimeError(
            "Not enough readable frames for visual verification."
        )

    return frame_paths


def _parse_visual_json(provider, raw_text):
    """Parse one strict JSON object from a vision provider."""
    try:
        verdict = extract_json_object(raw_text)
    except Exception as exc:
        preview = re.sub(r"\s+", " ", str(raw_text or "")).strip()[:350]
        raise VisionResponseError(
            f"{provider} did not return valid JSON: {exc}; response preview={preview!r}"
        ) from exc
    if not isinstance(verdict, dict):
        raise VisionResponseError(f"{provider} returned JSON, but it was not an object.")
    return verdict


def _parse_visual_verdict(provider, raw_text):
    """Parse and validate a strict visual-verification JSON response."""
    verdict = _parse_visual_json(provider, raw_text)
    relevant_value = verdict.get("relevant")

    if not isinstance(relevant_value, bool):
        raise VisionResponseError(
            f"{provider} JSON is missing a boolean 'relevant' field."
        )

    confidence_value = verdict.get("confidence")

    if isinstance(confidence_value, bool):
        raise VisionResponseError(
            f"{provider} returned a boolean instead of numeric confidence."
        )

    try:
        confidence = float(confidence_value)

    except (TypeError, ValueError) as exc:
        raise VisionResponseError(
            f"{provider} JSON is missing numeric 'confidence'."
        ) from exc

    if not 0.0 <= confidence <= 1.0:
        raise VisionResponseError(
            f"{provider} confidence must be between 0 and 1."
        )

    reason = re.sub(
        r"\s+",
        " ",
        str(
            verdict.get(
                "reason",
                "No visual evidence supplied."
            )
        )
    ).strip()

    return relevant_value, confidence, reason


def _verify_with_gemini(frames, prompt, api_key=None, return_json=False):
    parts = [{"text": prompt}]

    for frame in (frames if return_json else frames[:2]):
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(frame).decode("ascii"),
            }
        })

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{VISION_MODEL}:generateContent"
    )

    payload = {
        "contents": [
            {"parts": parts}
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 1200 if return_json else 512,
        },
    }

    effective_key = api_key or GEMINI_KEY
    response = post_with_transient_retry(
        url,
        retries=1,
        params={"key": effective_key},
        json=payload,
        timeout=(15, 55),
    )
    _observe_provider_response("Gemini", response, api_key=effective_key)

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")

        raise VisionProviderError(
            "Gemini",
            response.status_code,
            detail
        )

    try:
        data = response.json()

        raw_text = "\n".join(
            part.get("text", "")
            for part in data["candidates"][0]["content"]["parts"]
            if part.get("text")
        )

    except Exception as exc:
        raise VisionResponseError(
            f"Gemini visual verification returned an unreadable payload: "
            f"{exc}; body={response.text[:500]}"
        ) from exc

    return (
        _parse_visual_json("Gemini", raw_text)
        if return_json else _parse_visual_verdict("Gemini", raw_text)
    )


def _verify_with_cloudflare_vision(credentials, frames, prompt):
    """Verify a representative frame with Cloudflare Llama 3.2 Vision."""
    if not frames:
        raise VisionResponseError("Cloudflare Vision received no image frames.")
    frame = frames[len(frames) // 2]
    encoded = base64.b64encode(frame).decode("ascii")
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{credentials['account_id']}/ai/run/{CLOUDFLARE_VISION_MODEL}"
    )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict documentary footage reviewer. "
                    "Return exactly one JSON object with relevant, confidence, and reason."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "image": f"data:image/jpeg;base64,{encoded}",
        "max_tokens": 512,
        "temperature": 0.1,
    }
    response = post_with_transient_retry(
        url,
        retries=1,
        headers={
            "Authorization": f"Bearer {credentials['token']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(20, 75),
    )
    _observe_provider_response("Cloudflare", response, account_id=credentials["account_id"])
    if response.status_code >= 400:
        raise VisionProviderError(
            "Cloudflare Vision",
            response.status_code,
            response.text[:700].replace("\n", " "),
        )
    try:
        data = response.json()
        result = data.get("result", data)
        if isinstance(result, str):
            raw_text = result
        elif isinstance(result, dict):
            raw_text = (
                result.get("response")
                or result.get("output")
                or result.get("text")
                or ""
            )
        else:
            raw_text = ""
        if not raw_text:
            raise ValueError("Cloudflare Vision response did not contain result.response.")
    except Exception as exc:
        raise VisionResponseError(
            f"Cloudflare Vision returned an unreadable payload: {exc}; "
            f"body={response.text[:500]}"
        ) from exc
    return _parse_visual_verdict("Cloudflare Vision", raw_text)


def _verify_with_openai_compatible_vision(
    provider,
    api_key,
    model,
    frames,
    prompt,
    return_json=False,
):
    if provider == "Groq":
        url = (
            "https://api.groq.com/openai/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    else:
        url = (
            "https://openrouter.ai/api/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv(
                "OPENROUTER_SITE_URL",
                "https://github.com/"
            ),
            "X-Title": "Auto Documentary Video",
        }

    content = [
        {
            "type": "text",
            "text": prompt
        }
    ]

    for frame in frames:
        encoded = base64.b64encode(
            frame
        ).decode("ascii")

        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{encoded}"
            },
        })

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict documentary footage reviewer. "
                    "Return exactly one JSON object with relevant, confidence, "
                    "and reason. Do not add markdown or commentary."
                ),
            },
            {
                "role": "user",
                "content": content
            },
        ],
        "temperature": 0.1,
        "response_format": {
            "type": "json_object"
        },
    }

    if provider == "Groq":
        payload["max_completion_tokens"] = 1200 if return_json else 384

    else:
        payload["max_tokens"] = 1400 if return_json else 768
        payload["reasoning"] = {
            "enabled": False
        }

    response = post_with_transient_retry(
        url,
        retries=0,
        headers=headers,
        json=payload,
        timeout=(15, 65),
    )
    _observe_provider_response(provider, response, api_key=api_key, model=model)

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")

        raise VisionProviderError(
            provider,
            response.status_code,
            detail
        )

    try:
        data = response.json()

        message = data["choices"][0]["message"]
        message_content = message.get("content")

        if isinstance(message_content, list):
            text_parts = []

            for item in message_content:
                if isinstance(item, str):
                    text_parts.append(item)

                elif (
                    isinstance(item, dict)
                    and item.get("text")
                ):
                    text_parts.append(
                        str(item["text"])
                    )

            raw_text = "\n".join(
                text_parts
            ).strip()

        else:
            raw_text = str(
                message_content or ""
            ).strip()

        if not raw_text:
            choice = data.get("choices", [{}])[0]
            usage = data.get("usage", {})

            raise ValueError(
                "Empty response content "
                f"(model={data.get('model', model)!r}, "
                f"finish_reason={choice.get('finish_reason')!r}, "
                f"completion_tokens={usage.get('completion_tokens')!r})."
            )

    except Exception as exc:
        raise VisionResponseError(
            f"{provider} visual verification returned an unreadable response: "
            f"{exc}; body={response.text[:500]}"
        ) from exc

    return (
        _parse_visual_json(provider, raw_text)
        if return_json else _parse_visual_verdict(provider, raw_text)
    )


def _provider_retry_delay(error_text, default_seconds=60):
    """Read retry timing from common rate-limit error messages."""
    text = str(error_text or "")

    patterns = (
        r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*s",
        r"retry[- ]after[\s:=]+([0-9]+(?:\.[0-9]+)?)",
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            try:
                return max(
                    5,
                    min(180, float(match.group(1)))
                )
            except ValueError:
                pass

    return float(default_seconds)


def _provider_cooldown_remaining(provider_id):
    return max(
        0.0,
        VISION_PROVIDER_COOLDOWN_UNTIL.get(
            provider_id,
            0.0
        ) - time.monotonic(),
    )



def extract_crop_analysis_frames(video_path, frame_count=SMART_CROP_FRAME_COUNT):
    """Extract timestamped frames for AI subject-box tracking."""
    duration = ffprobe_duration(video_path)
    if duration < 1.0:
        raise RuntimeError("Landscape candidate is too short for safe subject tracking.")
    count = max(4, min(8, int(frame_count)))
    fractions = [0.10 + (0.80 * index / (count - 1)) for index in range(count)]
    frames = []
    for fraction in fractions:
        timestamp = min(max(0.05, duration * fraction), max(0.05, duration - 0.05))
        frame_path = None
        with tempfile.TemporaryDirectory(prefix="smart_crop_frame_") as temp_dir:
            frame_path = Path(temp_dir) / "frame.jpg"
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{timestamp:.3f}", "-i", str(video_path),
                    "-frames:v", "1", "-vf", "scale=768:-2:flags=lanczos",
                    "-q:v", "4", str(frame_path),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if result.returncode != 0 or not frame_path.is_file():
                raise RuntimeError(
                    "Could not extract smart-crop analysis frame: "
                    + (result.stderr or "FFmpeg produced no frame.")[-500:]
                )
            frames.append({"t": round(timestamp, 4), "image": frame_path.read_bytes()})
    if len(frames) < 4:
        raise RuntimeError("Not enough frames were available for dynamic subject tracking.")
    return frames


def _crop_vision_prompt(scene, query, metadata, frame_times, source_info):
    times = ", ".join(f"{value:.3f}" for value in frame_times)
    return f"""You are a precise video reframing analyst. The attached images are chronological frames from one landscape video.

DOCUMENTARY SCENE PRIMARY VISUAL: {scene.get('visual_query') or query}
SEARCH VARIANT: {query}
NARRATION CONTEXT: {scene.get('narration', '')}
SOURCE METADATA: {str(metadata or 'Not supplied')[:900]}
SOURCE DISPLAY DIMENSIONS: {source_info.get('width')}x{source_info.get('height')} (aspect {source_info.get('aspect_ratio')})
TARGET FRAME: portrait 9:16, using a horizontal window that spans the full source height.
FRAME TIMESTAMPS IN ATTACHMENT ORDER (seconds): [{times}]

TASK:
1. Identify the main visual subject AND all scene-critical elements needed to preserve the requested meaning. For a group, include the whole group; for a broad aerial canopy or landscape, include the area that makes the shot type recognizable.
2. For every attached frame, return one normalized bounding box [x1,y1,x2,y2] from 0 to 1 around that entire required visual content in the ORIGINAL frame. Coordinates are normalized to the full image: origin top-left. Do not return a box for a tiny arbitrary detail while ignoring other required scene elements.
3. Classify the shot in subject_kind. Use "discrete" when the scene is built around one object or group that must stay whole in frame (an animal, a person, a vehicle, a single nest, a machine). Use "extended" when the frame is itself the subject and no single element must stay whole at one instant: landscape, aerial or canopy views, horizons, weather, textures, migration lines, trails, crowds, fire fronts.
   - discrete: a single 9:16 window at each time must contain the whole bounding box plus safe margin. If the subject is too wide, clipped by the edge, not visible, or cannot stay in one vertical window, set composition_fits=false.
   - extended: the vertical window may traverse the scene with a smooth horizontal pan, because the narration describes the whole expanse. Set composition_fits=false only when the content cannot be traversed smoothly within the pan-speed limit, or extends beyond the source edge.
4. Track the same subject/composition across time. Bounding boxes must follow movement frame by frame, not stay fixed at image center.
5. Be conservative. If you cannot identify the requested content reliably, set subject_visible=false or composition_fits=false. Never invent off-frame positions.

Return ONLY JSON with exactly this structure:
{{"subject":"short subject description","subject_kind":"discrete","subject_visible":true,"composition_fits":true,"reason":"brief reason","frames":[{{"bbox":[0.20,0.10,0.42,0.80],"confidence":0.90}}]}}

The frames array MUST contain exactly {len(frame_times)} boxes, in the same order as the attached images. Every bbox coordinate must be numeric from 0 to 1 and satisfy x1<x2 and y1<y2. Confidence must be 0 to 1. Do not include markdown."""


def validate_smart_crop_plan(payload, source_info, frame_times):
    """Validate AI boxes and derive a smooth, safe 9:16 tracking path."""
    if not isinstance(payload, dict):
        raise RuntimeError("Crop planner returned a non-object response.")
    if payload.get("subject_visible") is not True or payload.get("composition_fits") is not True:
        raise RuntimeError(
            "Required scene content cannot be kept inside one vertical crop: "
            + str(payload.get("reason", "AI crop-fit check rejected the composition"))[:240]
        )
    raw_frames = payload.get("frames")
    if not isinstance(raw_frames, list) or len(raw_frames) != len(frame_times):
        raise RuntimeError("Crop planner did not return one bounding box for every sampled frame.")
    subject_kind = str(payload.get("subject_kind") or "").strip().lower()
    if subject_kind not in ("discrete", "extended"):
        subject_kind = ""

    width = int(source_info.get("width") or 0)
    height = int(source_info.get("height") or 0)
    aspect = float(source_info.get("aspect_ratio") or (width / max(1, height)))
    if not is_smart_reframe_eligible(width, height, aspect):
        raise RuntimeError("Downloaded source is outside the supported landscape reframing dimensions.")

    crop_fraction = TARGET_SOURCE_ASPECT / aspect
    if crop_fraction <= 0 or crop_fraction >= 1:
        raise RuntimeError("Invalid 9:16 crop window for this source aspect ratio.")
    margin = crop_fraction * SMART_CROP_BOX_MARGIN_FRACTION
    extended_frames_used = 0
    points = []
    desired_starts = []
    feasible_ranges = []

    for index, (frame, timestamp) in enumerate(zip(raw_frames, frame_times)):
        if not isinstance(frame, dict):
            raise RuntimeError(f"Crop planner frame {index + 1} is malformed.")
        bbox = frame.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise RuntimeError(f"Crop planner frame {index + 1} has no four-value bbox.")
        try:
            x1, y1, x2, y2 = [float(value) for value in bbox]
            confidence = float(frame.get("confidence", 0.0))
        except (TypeError, ValueError):
            raise RuntimeError(f"Crop planner frame {index + 1} contains non-numeric coordinates.")
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise RuntimeError(f"Crop planner frame {index + 1} has out-of-range bounding coordinates.")
        if not SMART_CROP_MIN_BOX_CONFIDENCE <= confidence <= 1:
            raise RuntimeError(f"Crop planner frame {index + 1} confidence is too low ({confidence:.2f}).")
        box_width = x2 - x1
        box_height = y2 - y1
        # An extended scene (aerial canopy, horizon, trail, crowd) cannot fit a
        # single vertical window at one instant, and does not have to: the shot
        # is the expanse, and the crop pans across it while the narration runs.
        # A discrete subject must still stay whole, which is what keeps the
        # fail-closed rule against cutting off scene-critical content.
        frame_kind = subject_kind
        if not frame_kind:
            # Without an explicit classification, only a box that covers almost
            # the whole frame (an aerial, horizon or texture shot) counts as an
            # extended scene. Anything smaller keeps the strict rule, so a wide
            # subject group is never panned away from.
            frame_kind = (
                "extended"
                if box_width > crop_fraction and box_width >= 0.85 and box_height >= 0.70
                else "discrete"
            )
        extended_frame = frame_kind == "extended"
        if not extended_frame and box_width + 2 * margin > crop_fraction:
            raise RuntimeError(
                f"Required visual content is too wide for 9:16 at sample {index + 1}; "
                "the generator will not cut off scene-critical subjects."
            )

        if extended_frame:
            # The window must stay inside the extended content and is free to
            # traverse it, so the feasible interval is the traversable span.
            low = max(0.0, min(1.0 - crop_fraction, x1))
            high = min(1.0 - crop_fraction, x2 - crop_fraction)
            if low > high:
                raise RuntimeError(
                    f"Required visual content touches an unsafe edge at sample {index + 1}."
                )
            extended_frames_used += 1
        else:
            # Feasible crop-left interval that keeps the full bbox plus safety margin.
            low = max(0.0, x2 + margin - crop_fraction)
            high = min(1.0 - crop_fraction, x1 - margin)
            if low > high:
                raise RuntimeError(
                    f"Required visual content touches an unsafe edge at sample {index + 1}."
                )
        center = (x1 + x2) / 2.0
        preferred = center - crop_fraction / 2.0
        preferred = max(low, min(high, preferred))
        desired_starts.append(preferred)
        feasible_ranges.append((low, high))
        points.append({
            "t": float(timestamp),
            "bbox": [round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5)],
            "confidence": round(confidence, 4),
        })

    # A short moving average smooths jitter; each result is clamped back into
    # the frame-specific interval that guarantees full subject containment.
    smoothed = []
    for index, value in enumerate(desired_starts):
        neighborhood = desired_starts[max(0, index - 1):min(len(desired_starts), index + 2)]
        average = sum(neighborhood) / len(neighborhood)
        low, high = feasible_ranges[index]
        smoothed.append(max(low, min(high, average)))

    for index, value in enumerate(smoothed):
        points[index]["x"] = round(value, 6)
        if index:
            dt = max(0.001, points[index]["t"] - points[index - 1]["t"])
            pan_rate = abs(value - smoothed[index - 1]) / dt
            if pan_rate > SMART_CROP_MAX_PAN_RATE_PER_SECOND:
                raise RuntimeError(
                    "The required subject moves too abruptly for a safe, smooth vertical crop."
                )

    return {
        "mode": "ai_content_aware_dynamic_crop",
        "target_aspect": "9:16",
        "subject_kind": "extended" if extended_frames_used else "discrete",
        "extended_scene_frames": extended_frames_used,
        "source_width": width,
        "source_height": height,
        "source_aspect_ratio": round(aspect, 6),
        "crop_width_fraction": round(crop_fraction, 6),
        "subject": re.sub(r"\s+", " ", str(payload.get("subject", "scene subject"))).strip()[:120],
        "reason": re.sub(r"\s+", " ", str(payload.get("reason", ""))).strip()[:300],
        "points": points,
    }


def crop_x_at_time(crop_plan, timestamp):
    points = crop_plan.get("points") if isinstance(crop_plan, dict) else None
    if not isinstance(points, list) or len(points) < 2:
        raise RuntimeError("Smart crop plan has insufficient tracking points.")
    t = float(timestamp)
    if t <= float(points[0]["t"]):
        return float(points[0]["x"])
    if t >= float(points[-1]["t"]):
        return float(points[-1]["x"])
    for left, right in zip(points, points[1:]):
        t0, t1 = float(left["t"]), float(right["t"])
        if t0 <= t <= t1:
            ratio = (t - t0) / max(0.001, t1 - t0)
            return float(left["x"]) + (float(right["x"]) - float(left["x"])) * ratio
    return float(points[-1]["x"])


def smart_crop_geometry(source_info, crop_plan):
    """Return even crop dimensions and a safe FFmpeg x expression."""
    width = int(source_info.get("width") or 0)
    height = int(source_info.get("height") or 0)
    if width <= height or height < MIN_REFRAME_SOURCE_HEIGHT:
        raise RuntimeError("Smart crop requires a supported landscape source.")
    if int(crop_plan.get("source_width") or 0) != width or int(crop_plan.get("source_height") or 0) != height:
        raise RuntimeError("Smart crop plan dimensions do not match the downloaded source.")
    crop_width = int(round((height * TARGET_SOURCE_ASPECT) / 2.0)) * 2
    crop_height = height - (height % 2)
    if crop_width < 2 or crop_width > width:
        raise RuntimeError("Smart crop geometry is invalid for the downloaded source.")
    if not isinstance(crop_plan.get("points"), list) or len(crop_plan["points"]) < 2:
        raise RuntimeError("Smart crop plan is missing tracking points.")

    xs = [max(0.0, min(1.0 - crop_width / width, float(p["x"]))) * width for p in crop_plan["points"]]
    times = [float(p["t"]) for p in crop_plan["points"]]
    source_duration = float(crop_plan.get("source_duration") or 0.0)
    time_expr = f"mod(t,{source_duration:.4f})" if source_duration > 0 else "t"
    expression = f"{xs[-1]:.3f}"
    for index in range(len(xs) - 2, -1, -1):
        dt = max(0.001, times[index + 1] - times[index])
        interpolation = (
            f"{xs[index]:.3f}+({xs[index + 1]:.3f}-{xs[index]:.3f})"
            f"*({time_expr}-{times[index]:.4f})/{dt:.4f}"
        )
        expression = f"if(lt({time_expr},{times[index + 1]:.4f}),{interpolation},{expression})"
    expression = f"if(lt({time_expr},{times[0]:.4f}),{xs[0]:.3f},{expression})"
    return crop_width, crop_height, expression


def smart_crop_filter(source_info, crop_plan, output_width=768):
    crop_width, crop_height, x_expression = smart_crop_geometry(source_info, crop_plan)
    return (
        f"crop={crop_width}:{crop_height}:x='{x_expression}':y=0:exact=1,"
        f"scale={output_width}:-2:flags=lanczos,setsar=1"
    )


def extract_smart_crop_verification_frames(video_path, source_info, crop_plan):
    """Extract three frames through the proposed moving crop for post-crop AI review."""
    duration = ffprobe_duration(video_path)
    fractions = (0.20, 0.50, 0.80)
    crop_width, crop_height, _expression = smart_crop_geometry(source_info, crop_plan)
    frames = []
    for fraction in fractions:
        timestamp = min(max(0.1, duration * fraction), max(0.1, duration - 0.1))
        x_fraction = crop_x_at_time(crop_plan, timestamp)
        x_pixels = int(round(x_fraction * int(source_info["width"])))
        x_pixels = max(0, min(int(source_info["width"]) - crop_width, x_pixels))
        crop_filter = (
            f"crop={crop_width}:{crop_height}:{x_pixels}:0:exact=1,"
            "scale=768:-2:flags=lanczos,setsar=1"
        )
        with tempfile.TemporaryDirectory(prefix="smart_crop_verify_") as temp_dir:
            frame_path = Path(temp_dir) / "crop.jpg"
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{timestamp:.3f}", "-i", str(video_path),
                    "-frames:v", "1", "-vf", crop_filter,
                    "-q:v", "4", str(frame_path),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if result.returncode != 0 or not frame_path.is_file():
                raise RuntimeError(
                    "Could not extract post-crop verification frame: "
                    + (result.stderr or "FFmpeg produced no frame.")[-500:]
                )
            frames.append(frame_path.read_bytes())
    return frames


def analyze_smart_crop(video_path, scene, query, candidate_metadata, source_info):
    """Ask available quota-aware vision models for conservative subject boxes."""
    sampled = extract_crop_analysis_frames(video_path)
    frame_times = [item["t"] for item in sampled]
    frames = [item["image"] for item in sampled]
    prompt = _crop_vision_prompt(scene, query, candidate_metadata, frame_times, source_info)
    routes = []

    if GROQ_KEYS:
        for key_index, api_key in enumerate(GROQ_KEYS, 1):
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:10]
            routes.append(_quota_route(
                "Groq", f"Groq crop tracker key {key_index} ({GROQ_VISION_MODEL})", api_key,
                lambda api_key=api_key: _verify_with_openai_compatible_vision(
                    "Groq", api_key, GROQ_VISION_MODEL, frames, prompt, return_json=True
                ),
                route_id=f"GroqCrop::{key_hash}",
            ))

    if OPENROUTER_KEYS:
        models = []
        for model_name in (OPENROUTER_VISION_MODEL, OPENROUTER_VISION_FALLBACK_MODEL):
            if model_name and model_name not in models:
                models.append(model_name)
        for key_index, api_key in enumerate(OPENROUTER_KEYS, 1):
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:10]
            for model_name in models:
                routes.append(_quota_route(
                    "OpenRouter", f"OpenRouter crop tracker key {key_index} ({model_name})", api_key,
                    lambda api_key=api_key, model_name=model_name: _verify_with_openai_compatible_vision(
                        "OpenRouter", api_key, model_name, frames, prompt, return_json=True
                    ),
                    route_id=f"OpenRouterCrop::{key_hash}::{model_name}",
                    model=model_name,
                ))

    for key_index, api_key in enumerate(GEMINI_KEYS, 1):
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:10]
        routes.append(_quota_route(
            "Gemini", f"Gemini crop tracker key {key_index} ({VISION_MODEL})", api_key,
            lambda api_key=api_key: _verify_with_gemini(
                frames, prompt, api_key=api_key, return_json=True
            ),
            route_id=f"GeminiCrop::{key_hash}",
        ))

    if not routes:
        raise RuntimeError("No multi-frame vision provider is available for safe landscape reframing.")

    errors = []
    pending = list(routes)
    while pending:
        ranked = QUOTA_ROUTER.rank_routes(pending)
        if not ranked:
            break
        route = ranked[0]
        pending.remove(route)
        QUOTA_ROUTER.mark_selected(route)
        try:
            payload = route["run"]()
            plan = validate_smart_crop_plan(payload, source_info, frame_times)
            plan["source_duration"] = round(ffprobe_duration(video_path), 4)
            plan["vision_provider"] = route["name"]
            print(
                f"Smart crop planner: {route['name']} tracked {len(plan['points'])} frames "
                f"for {plan['subject']!r}; all boxes fit inside 9:16."
            )
            return plan
        except VisionProviderError as exc:
            detail = redact_provider_error(str(exc), GROQ_KEYS + OPENROUTER_KEYS + GEMINI_KEYS, max_chars=500)
            errors.append(detail)
            if exc.status_code in (408, 425, 429, 500, 502, 503, 504):
                VISION_PROVIDER_COOLDOWN_UNTIL[route["id"]] = time.monotonic() + _provider_retry_delay(detail)
            else:
                VISION_DISABLED_PROVIDERS.add(route["id"])
        except VisionResponseError as exc:
            errors.append(str(exc)[:350])
        except RuntimeError:
            # A valid AI decision that the subject does not fit is a candidate rejection,
            # not a provider failure and must not be bypassed with a different model.
            raise
        except Exception as exc:
            errors.append(str(exc)[:350])

    raise RuntimeError(
        "No vision provider produced a safe dynamic crop plan. "
        + " | ".join(errors[:3])
    )

def verify_footage_visually(
    video_path,
    scene,
    query,
    candidate_metadata="",
    frames_override=None,
):
    """Verify visible shot content, using catalog metadata for provenance."""
    frames = (
        list(frames_override)
        if frames_override is not None
        else extract_vision_frames(video_path, frame_count=3)
    )
    if len(frames) < 2:
        raise VisionResponseError("Visual verification needs at least two usable frames.")

    metadata_context = re.sub(
        r"\s+",
        " ",
        str(candidate_metadata or "")
    ).strip()[:1200]

    prompt = f"""You are a strict documentary footage verifier.
Inspect the attached frames sampled from one candidate video clip.

SCENE PRIMARY VISUAL QUERY: {scene.get('visual_query') or query}
CANDIDATE SEARCH VARIANT: {query}
SCENE NARRATION: {scene.get('narration', '')}
SOURCE CATALOG METADATA: {metadata_context or 'Not supplied'}

IMPORTANT DISTINCTION:
- Judge visible subject, composition, environment and camera viewpoint from the images.
- Use explicit source catalog metadata as supporting provenance for geographic identity.
- Do NOT demand that an image visually proves an exact country, river basin, or rainforest name. A genuine Amazon rainforest clip cannot always be distinguished geographically from another tropical rainforest by pixels alone.
- However, metadata alone cannot turn an unrelated image into a match. The visible footage must still show the requested type of scene.

VISUAL MATCH RULES:
- The SCENE PRIMARY VISUAL QUERY is authoritative. A fallback search phrase may change wording, but it may not replace the main subject, requested shot type, or defining visual condition.
- The main concrete subject named in the primary query must be visible. For example, a jaguar primary query must show a jaguar; a rainforest-canopy-aerial primary query must show a broad overhead rainforest canopy; a raindrop-on-window primary query must show rain/water drops on glass.
- A generic forest, green foliage, wildlife, or atmospheric shot is NOT a match for a more specific primary query.
- Alternative shot details may support the narration, but they cannot be used to bypass the primary visual intent.
- Weather and mood words such as rain, mist, wet, or sunrise are supporting context. Do not reject a clearly matching subject only because a brief weather condition is not obvious in two or three sampled frames.
- If the primary query asks for an aerial/drone canopy view, the frames must show a broad overhead view of forest treetops. A ground-level path, close-up leaves, isolated branches, hills, or a stylized animation is not a match.
- If the primary query asks for a specific phenomenon (for example deforestation, rainfall, or a moisture plume), unrelated phenomena with similar shapes (such as volcanic ash clouds) must be rejected.
- Judge the requested shot type and visible subject before guessing a region or tree species. Do not label a canopy as temperate/European merely because the exact location is not visually identifiable.
- Use source catalog metadata for geographic provenance. If metadata supports a tropical/rainforest setting and the frames show compatible dense green forest canopy, accept the visual match without demanding that the exact country be visible.
- If the catalog explicitly identifies a conflicting setting (for example, a manicured park or temperate autumn woodland), reject it. Never describe a generic canopy clip as confirmed Amazon footage unless the source metadata supports that claim.
- The narration provides context, but do not require every fact in the narration to appear in one clip.
- Do not infer the main subject if it is not visible. If the frames contradict the requested shot, reject.

Return ONLY JSON:
{{"relevant": true, "confidence": 0.0, "reason": "brief visible evidence and metadata basis"}}

Set relevant=true only when the visual composition genuinely matches the SCENE PRIMARY VISUAL QUERY and source metadata supports any specific geographic claim. If only a broad setting matches, or the main requested subject/condition is absent, set false. If uncertain about the actual shot, set false. Confidence must be a number from 0 to 1."""

    providers = []

    if GROQ_KEYS:
        groq_frames = [frames[len(frames) // 2]]
        for key_index, api_key in enumerate(GROQ_KEYS, 1):
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:10]
            providers.append(_quota_route(
                "Groq", f"Groq key {key_index} ({GROQ_VISION_MODEL})", api_key,
                lambda api_key=api_key: _verify_with_openai_compatible_vision(
                    "Groq", api_key, GROQ_VISION_MODEL, groq_frames, prompt
                ),
                route_id=f"Groq::{key_hash}",
            ))

    if OPENROUTER_KEYS:
        models = []
        for model_name in (OPENROUTER_VISION_MODEL, OPENROUTER_VISION_FALLBACK_MODEL):
            if model_name and model_name not in models:
                models.append(model_name)
        openrouter_frames = [frames[0], frames[-1]] if len(frames) > 1 else frames
        for key_index, api_key in enumerate(OPENROUTER_KEYS, 1):
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:10]
            for model_name in models:
                providers.append(_quota_route(
                    "OpenRouter", f"OpenRouter key {key_index} ({model_name})", api_key,
                    lambda api_key=api_key, model_name=model_name: _verify_with_openai_compatible_vision(
                        "OpenRouter", api_key, model_name, openrouter_frames, prompt
                    ),
                    route_id=f"OpenRouter::{key_hash}::{model_name}",
                    model=model_name,
                ))

    gemini_frames = [frames[0], frames[-1]] if len(frames) > 1 else frames
    for key_index, api_key in enumerate(GEMINI_KEYS, 1):
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:10]
        providers.append(_quota_route(
            "Gemini", f"Gemini key {key_index} ({VISION_MODEL})", api_key,
            lambda api_key=api_key: _verify_with_gemini(gemini_frames, prompt, api_key=api_key),
            route_id=f"Gemini::{key_hash}",
        ))

    for key_index, credentials in enumerate(CLOUDFLARE_CREDENTIALS, 1):
        pair_hash = hashlib.sha256(
            (credentials["token"] + ":" + credentials["account_id"]).encode()
        ).hexdigest()[:10]
        providers.append(_quota_route(
            "Cloudflare", f"Cloudflare Vision key pair {key_index} ({CLOUDFLARE_VISION_MODEL})",
            credentials["token"],
            lambda credentials=credentials: _verify_with_cloudflare_vision(credentials, frames, prompt),
            account_id=credentials["account_id"],
            route_id=f"Cloudflare::{pair_hash}",
        ))

    errors = []
    attempted = 0
    pending = list(providers)
    route_attempts = {}
    max_route_attempts = max(1, min(4, int(os.getenv("VISION_MAX_ROUTE_ATTEMPTS", "3"))))
    recovery_wait_budget = max(
        0.0,
        min(600.0, float(os.getenv("VISION_FAILOVER_WAIT_BUDGET_SECONDS", "180"))),
    )
    recovery_wait_used = 0.0

    def route_cooldown_remaining(candidate_route):
        """Include both local verifier and quota-router cooldown clocks."""
        candidate_id = candidate_route["id"]
        now = time.monotonic()
        remaining = _provider_cooldown_remaining(candidate_id)
        for scope_name in (
            candidate_route.get("scope"),
            candidate_route.get("shared_scope"),
            candidate_route.get("model_scope"),
        ):
            if not scope_name:
                continue
            pool_state = QUOTA_ROUTER.pools.get(str(scope_name), {})
            remaining = max(
                remaining,
                float(pool_state.get("cooldown_until", 0.0)) - now,
            )
        return max(0.0, remaining)

    def safe_error(exc):
        known_secrets = list(GROQ_KEYS) + list(OPENROUTER_KEYS) + list(GEMINI_KEYS)
        for item in CLOUDFLARE_CREDENTIALS:
            known_secrets.extend((item.get("token", ""), item.get("account_id", "")))
        known_secrets.extend(SPEECHIFY_KEYS)
        return redact_provider_error(str(exc), known_secrets, max_chars=700)

    while pending:
        eligible = []
        for candidate_route in list(pending):
            candidate_id = candidate_route["id"]
            if candidate_id in VISION_DISABLED_PROVIDERS:
                pending.remove(candidate_route)
                continue
            if route_attempts.get(candidate_id, 0) >= max_route_attempts:
                pending.remove(candidate_route)
                continue
            if route_cooldown_remaining(candidate_route) > 0:
                continue
            eligible.append(candidate_route)

        ranked = QUOTA_ROUTER.rank_routes(eligible)
        if not ranked:
            # Temporary 429/5xx routes are re-queued. Wait for the earliest
            # advertised/reset cooldown instead of aborting immediately.
            waiting_routes = [
                candidate_route
                for candidate_route in pending
                if candidate_route["id"] not in VISION_DISABLED_PROVIDERS
                and route_attempts.get(candidate_route["id"], 0) < max_route_attempts
                and route_cooldown_remaining(candidate_route) > 0
            ]
            if waiting_routes:
                wait_seconds = min(route_cooldown_remaining(item) for item in waiting_routes)
                budget_left = recovery_wait_budget - recovery_wait_used
                if wait_seconds <= budget_left:
                    actual_wait = wait_seconds + 0.25
                    print(
                        "All remaining visual routes are temporarily cooling down; "
                        f"waiting {actual_wait:.1f}s for quota/service recovery "
                        f"(budget remaining {budget_left:.1f}s)."
                    )
                    time.sleep(actual_wait)
                    recovery_wait_used += actual_wait
                    continue
                errors.append(
                    f"Visual failover recovery wait budget exhausted; "
                    f"next provider reset is in {wait_seconds:.1f}s."
                )
            break

        route = ranked[0]
        pending.remove(route)
        provider_id = route["id"]
        provider_name = route["name"]
        verify = route["run"]
        route_attempts[provider_id] = route_attempts.get(provider_id, 0) + 1
        attempt_number = route_attempts[provider_id]
        QUOTA_ROUTER.mark_selected(route)
        attempted += 1
        print(
            f"Visual verifier: quota-aware selection -> {provider_name} "
            f"(attempt {attempt_number}/{max_route_attempts})..."
        )

        try:
            relevant, confidence, reason = verify()

            accepted = (
                relevant
                and confidence >= VISION_MIN_CONFIDENCE
            )

            print(
                f"Visual verifier: {provider_name} returned "
                f"{'PASS' if accepted else 'REJECT'} "
                f"({confidence:.2f})."
            )

            return (
                accepted,
                confidence,
                reason,
                provider_name
            )

        except VisionProviderError as exc:
            detail = safe_error(exc)
            errors.append(detail)
            should_retry, delay, retry_reason = visual_retry_policy(
                provider_name,
                exc.status_code,
                detail,
                attempt_number=attempt_number,
                max_attempts=max_route_attempts,
            )

            if should_retry:
                now = time.monotonic()
                VISION_PROVIDER_COOLDOWN_UNTIL[provider_id] = now + delay
                # Cool down model siblings for the same OpenRouter key so an
                # exhausted model route does not cause pointless sibling calls.
                route_parts = provider_id.split("::")
                if provider_name.startswith("OpenRouter") and len(route_parts) >= 2:
                    route_group = "::".join(route_parts[:2])
                    for sibling_route in providers:
                        sibling_id = sibling_route["id"]
                        if sibling_id.startswith(route_group + "::"):
                            VISION_PROVIDER_COOLDOWN_UNTIL[sibling_id] = now + delay
                pending.append(route)
                print(
                    f"Visual verifier {provider_name} returned HTTP {exc.status_code}; "
                    f"{retry_reason}. It will be reconsidered after {delay:.0f}s. "
                    f"Detail: {detail[:500]}"
                )
            else:
                if exc.status_code in (400, 401, 402, 403, 404, 413, 415, 422):
                    VISION_DISABLED_PROVIDERS.add(provider_id)
                    print(
                        f"Visual verifier {provider_name} has a non-retryable "
                        f"configuration/auth error (HTTP {exc.status_code}); "
                        f"disabling this route. Detail: {detail[:650]}"
                    )
                else:
                    print(
                        f"Visual verifier {provider_name} will not be retried "
                        f"(HTTP {exc.status_code}; {retry_reason}). "
                        f"Detail: {detail[:500]}"
                    )

        except VisionResponseError as exc:
            detail = safe_error(exc)
            errors.append(detail)
            VISION_DISABLED_PROVIDERS.add(provider_id)
            print(
                f"Visual verifier {provider_name} returned unusable output; "
                f"disabling this route. Detail: {detail[:500]}"
            )

        except requests.exceptions.Timeout as exc:
            detail = safe_error(exc)
            errors.append(f"{provider_name}: timeout: {detail}")
            should_retry, delay, retry_reason = visual_retry_policy(
                provider_name,
                408,
                detail,
                attempt_number=attempt_number,
                max_attempts=max_route_attempts,
            )
            if should_retry:
                VISION_PROVIDER_COOLDOWN_UNTIL[provider_id] = time.monotonic() + delay
                pending.append(route)
            print(
                f"Visual verifier {provider_name} timed out; "
                f"{retry_reason}; wait {delay:.0f}s before retry."
                if should_retry else
                f"Visual verifier {provider_name} timed out and exhausted its retry budget."
            )

        except Exception as exc:
            detail = safe_error(exc)
            errors.append(f"{provider_name}: {detail}")
            should_retry, delay, retry_reason = visual_retry_policy(
                provider_name,
                503,
                detail,
                attempt_number=attempt_number,
                max_attempts=max_route_attempts,
            )
            if should_retry:
                VISION_PROVIDER_COOLDOWN_UNTIL[provider_id] = time.monotonic() + delay
                pending.append(route)
            print(
                f"Visual verifier {provider_name} failed; "
                f"{retry_reason}. Detail: {detail[:500]}"
            )

    if not providers:
        detail = (
            "No visual AI credentials are configured."
        )

    elif attempted == 0:
        detail = (
            "All visual AI routes are disabled or cooling down."
        )

    elif not errors:
        detail = (
            "No provider produced a usable visual-verification response."
        )

    else:
        detail = " | ".join(errors)

    raise VisionVerificationError(
        "All visual verification routes failed for this candidate. "
        + detail
    )


    return combined


def rank_footage_candidates_by_source(candidates):
    """Rank primarily by visual-intent relevance; trust tier breaks near-ties only."""
    candidates_by_source = {}
    for candidate_item in candidates:
        candidate_source = str(candidate_item[1].get("source") or "").strip()
        candidates_by_source.setdefault(candidate_source, []).append(candidate_item)

    source_order = {name: index for index, name in enumerate(FOOTAGE_SOURCE_PRIORITY)}
    capped = []
    for candidate_source, source_candidates in candidates_by_source.items():
        source_candidates.sort(key=lambda item: float(item[0]), reverse=True)
        source_limit = FOOTAGE_SOURCE_CANDIDATE_LIMITS.get(candidate_source, 4)
        capped.extend(source_candidates[:source_limit])

    # A 0.08 score band lets a more relevant clip beat source preference, while
    # keeping the more reliable open archives ahead when candidates are close.
    def sort_key(item):
        score = max(0.0, float(item[0]))
        hit = item[1]
        source = str(hit.get("source") or "").strip()
        trust_rank = source_order.get(source, len(source_order) + 1)
        # Within the same relevance band, try known native-portrait media first.
        # Other candidates remain eligible because catalog dimensions may omit
        # display rotation; their actual ratio is checked after download.
        portrait_hint = int(has_native_portrait_metadata_hint(hit))
        return (int(score / 0.08), portrait_hint, -trust_rank, score)

    return sorted(capped, key=sort_key, reverse=True)


def visual_rejection_context_groups(feedback, query, narration=""):
    """Identify metadata contexts the visual verifier rejected for this scene."""
    feedback_text = " ".join(
        str(item.get("reason", "") if isinstance(item, dict) else item)
        for item in (feedback or [])
    )
    feedback_tokens = set(normalize_words(feedback_text))
    requested_tokens = set(normalize_words(f"{query or ''} {narration or ''}"))
    return {
        name for name, terms in CONTEXT_CONFLICTS.items()
        if feedback_tokens & terms and not requested_tokens & terms
    }


def adaptive_visual_retry_queries(scene, rejection_feedback=None):
    """Create a small, subject-preserving second-wave search from failed scene intent."""
    primary = clean_query(scene.get("visual_query", ""))
    narration = str(scene.get("narration", "") or "")
    combined = f"{primary} {narration}"
    tokens = set(normalize_words(combined))
    _, groups = query_concepts(primary)
    group_names = set(groups)
    feedback_text = " ".join(
        str(item.get("reason", "") if isinstance(item, dict) else item)
        for item in (rejection_feedback or [])
    ).lower()
    feedback_tokens = set(normalize_words(feedback_text))

    queries = []
    seen = set()

    def add(value):
        cleaned = clean_query(value)
        normalized_tokens = list(dict.fromkeys(normalize_words(cleaned)))
        normalized = " ".join(normalized_tokens)
        key = normalized.lower()
        if normalized and key not in seen and key != " ".join(normalize_words(primary)).lower():
            seen.add(key)
            queries.append(normalized)

    wants_aerial = "aerial" in group_names or bool(tokens & CONCEPT_GROUPS.get("aerial", set()))
    wants_rainforest = "rainforest" in group_names
    wants_canopy = "canopy" in group_names

    if wants_rainforest and wants_canopy and wants_aerial:
        add("Amazon tropical rainforest continuous canopy drone aerial overhead perspective wide")
        add("Brazil rainforest treetops high altitude drone panorama natural forest canopy")
    elif wants_rainforest and wants_canopy:
        add("Amazon tropical rainforest treetop foliage canopy detailed close view")
        add("dense tropical jungle canopy leaves treetops natural habitat")
    elif wants_aerial and "river" in group_names:
        add("river winding through tropical forest drone aerial overhead")
        add("natural river course landscape overhead perspective aerial footage")
    elif wants_aerial and "forest" in group_names:
        add("remote natural forest canopy drone overhead wide aerial landscape")
        add("continuous woodland treetops overhead perspective aerial documentary")
    elif "jaguar" in group_names:
        add("wild jaguar moving in natural rainforest habitat camera trap")
        add("jaguar wildlife in dense tropical forest natural environment")
    elif "insect" in group_names:
        # Keep the scene's own subject in every fallback. "insect" alone matches
        # beetles, bees and crickets as readily as the ant the narration is about.
        add(f"{scene_subject_phrase(primary)} macro closeup on natural leaf detailed footage")
        add(f"wild {scene_subject_phrase(primary)} crawling on plant macro documentary video")
    elif "orchid" in group_names:
        add("wild tropical orchid flowering naturally on tree closeup")
        add("orchid epiphyte growing on rainforest tree macro footage")
    elif group_names & {"research_activity", "field_instrument"} or tokens & {
        "research", "researcher", "scientist", "measuring", "measurement", "sensor", "humidity"
    }:
        add("field scientist collecting environmental data in natural forest research station")
        add("researcher measuring humidity with portable sensor in tropical forest")
        add("outdoor weather station instruments environmental monitoring closeup")
    elif wants_aerial:
        add(f"{primary} drone aerial overhead perspective wide documentary footage")
        add(f"{primary} high altitude aerial camera wide landscape")
    else:
        add(f"{primary} authentic real world documentary video")
        add(f"{primary} close detailed natural environment footage")

    # A visual rejection can reveal the shot-type mismatch even when metadata did not.
    if wants_aerial and feedback_tokens & {
        "ground", "groundlevel", "path", "trail", "closeup", "close", "leaves", "branch"
    }:
        add(f"{primary} drone overhead perspective high altitude wide shot")

    # If the failed clip was an off-topic human activity, bias the next query
    # toward an unpopulated landscape while preserving the original anchors.
    if feedback_tokens & CONTEXT_CONFLICTS.get("military", set()) and wants_rainforest:
        add("remote Amazon rainforest unpopulated natural canopy drone aerial")

    return queries[:MAX_ADAPTIVE_FOOTAGE_QUERIES]


def footage_identity_signature(hit):
    """Stable clip identity for cross-scene duplicate detection."""
    url = str(hit.get("url") or "")
    filename = unquote(url.rsplit("/", 1)[-1]) if url else ""
    text = " ".join([str(hit.get("title") or ""), filename])
    tokens = [
        token for token in normalize_words(text)
        if token not in {
            "video", "videos", "webm", "mp4", "mov", "ogv", "file", "the",
            "and", "of", "a", "an", "in", "on", "with", "clip", "footage",
        }
    ]
    return " ".join(sorted(set(tokens)))


def fetch_footage(plan):
    credits = []
    used_ids = set()
    selected_tag_sets = []
    verified_assets = []
    footage_audit = []
    accepted_signatures = set()
    license_rejections = []
    license_required = os.getenv("FOOTAGE_LICENSE_REQUIRED", "true").strip().lower() not in (
        "0", "false", "no", "off"
    )
    configure_footage_budget(len(plan.get("scenes") or []))

    visual_provider_names = []

    if GROQ_KEYS:
        visual_provider_names.append(
            f"Groq ({len(GROQ_KEYS)} key(s), {GROQ_VISION_MODEL})"
        )

    if OPENROUTER_KEYS:
        visual_provider_names.append(
            f"OpenRouter ({len(OPENROUTER_KEYS)} key(s), {OPENROUTER_VISION_MODEL})"
        )
        if OPENROUTER_VISION_FALLBACK_MODEL != OPENROUTER_VISION_MODEL:
            visual_provider_names.append(
                f"OpenRouter backup model ({OPENROUTER_VISION_FALLBACK_MODEL})"
            )

    if GEMINI_KEYS:
        visual_provider_names.append(
            f"Gemini ({len(GEMINI_KEYS)} key(s), {VISION_MODEL})"
        )

    if CLOUDFLARE_CREDENTIALS:
        visual_provider_names.append(
            f"Cloudflare Workers AI ({len(CLOUDFLARE_CREDENTIALS)} key pair(s), {CLOUDFLARE_VISION_MODEL})"
        )

    if VISION_REQUIRED and not visual_provider_names:
        raise RuntimeError(
            "Stage 1 requires visual verification. Add at least one of "
            "GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY, or a Cloudflare token/account pair under "
            "GitHub Settings > Secrets and variables > Actions."
        )

    discovery_priority = (
        "Coverr native-vertical API -> " if ENABLE_COVERR_SOURCE else ""
    ) + (
        "Internet Archive -> Wikimedia Commons -> NASA -> "
        "Google Search direct reusable-video files"
    )
    print(
        "Footage discovery priority: " + discovery_priority + ". "
        + (
            "Coverr vertical/free/non-AI search is enabled via COVERR_API_KEY. "
            if ENABLE_COVERR_SOURCE else
            "Coverr is skipped because COVERR_API_KEY is not configured. "
        )
        + "Pexels/Pixabay remain disabled unless explicitly enabled."
    )

    if not visual_provider_names:
        print(
            "WARNING: FOOTAGE_VISION_REQUIRED=false and no visual AI keys "
            "are configured; using metadata-only footage checks."
        )

    else:
        print(
            "Visual verification enabled with automatic failover: "
            + " -> ".join(visual_provider_names)
            + f". Minimum confidence={VISION_MIN_CONFIDENCE:.2f}."
        )

    for i, scene in enumerate(plan["scenes"], 1):
        scene["visual_type"] = "footage"

        raw_queries = [
            scene.get("visual_query", "")
        ]

        raw_queries.extend(
            list(scene.get("visual_queries", [])[:2])
        )

        queries = expand_visual_queries(
            raw_queries,
            scene.get("narration", ""),
        )

        # MM7: if a provider's remaining allowance is nearly spent, search fewer
        # variants for this scene so the last scenes still have discovery budget.
        tight_sources = [
            source for source in (
                "Wikimedia Commons", "Internet Archive", "NASA", "Web Search"
            )
            if 0 < footage_source_budget_remaining(source) < 6
        ]
        if tight_sources and len(queries) > 3:
            queries = queries[:3]
            print(
                f"Scene {i}: request budget is nearly exhausted for "
                f"{', '.join(tight_sources)}; limiting this scene to "
                f"{len(queries)} query variants so later scenes keep their allowance."
            )

        candidates = {}

        print(
            f"Scene {i}: applying metadata anchors independently to "
            f"each of {len(queries)} search variants."
        )

        strict_candidate_count = 0
        relaxed_candidate_count = 0
        rejection_counts = {
            "metadata": 0, "resolution": 0,
            "duration": 0, "relevance": 0, "context": 0,
            "visual_context": 0, "duplicate": 0, "license": 0,
        }
        visual_rejection_feedback = []
        scene_vision_rejections = []
        scene_license_rejections = []
        rejected_context_groups = set()
        # Candidates whose metadata only partly names the scene subject. They are
        # not accepted here; they are shown to the visual model, which decides.
        pending_vision = {}

        def collect_query_candidates(
            query, allow_relaxed=False, include_coverr=False
        ):
            nonlocal strict_candidate_count, relaxed_candidate_count
            if include_coverr and ENABLE_COVERR_SOURCE:
                source_hits = search_all_footage_sources(
                    query, include_coverr=True
                )
            else:
                # Keep compatibility with the pre-MM4 search function signature
                # used by downstream test harnesses and local extensions.
                source_hits = search_all_footage_sources(query)
            for hit in source_hits:
                identity = f"{hit.get('source')}:{hit.get('id')}"

                if not hit.get("url") or identity in used_ids:
                    continue

                candidate_tokens = set(normalize_words(" ".join([
                    str(hit.get("title", "")),
                    str(hit.get("tags", "")),
                    str(hit.get("metadata", "")),
                ])))
                if any(
                    candidate_tokens & CONTEXT_CONFLICTS.get(group, set())
                    for group in rejected_context_groups
                ):
                    rejection_counts["visual_context"] += 1
                    continue

                strict_match = candidate_matches_visual_anchors(
                    hit, query, primary_query=scene.get("visual_query")
                )
                if not strict_match:
                    if not allow_relaxed or not candidate_matches_relaxed_metadata(
                        hit, query, scene.get("narration", ""), min_score=0.08
                    ):
                        if (
                            visual_provider_names
                            and len(pending_vision) < VISION_CANDIDATE_LIMIT
                            and candidate_partial_subject_match(hit, query)
                        ):
                            pending_vision[identity] = (
                                *relevance_score(hit, query, scene.get("narration", "")),
                                hit,
                                query,
                            )
                        rejection_counts["metadata"] += 1
                        continue
                    relaxed_candidate_count += 1
                else:
                    strict_candidate_count += 1

                duration = float(hit.get("duration") or 0)
                if candidate_metadata_precheck(hit) == "resolution":
                    rejection_counts["resolution"] += 1
                    continue
                # Do not reject catalog-reported landscape dimensions here.
                # Some providers expose encoded dimensions but omit 90-degree
                # display-rotation metadata. ffprobe after download is the
                # authoritative native-9:16 check and still rejects true landscape.
                if duration and duration < 4:
                    rejection_counts["duration"] += 1
                    continue

                score, matched, total = relevance_score(
                    hit, query, scene.get("narration", "")
                )
                minimum_score = 0.10 if not strict_match else 0.20
                if matched < 1 or score < minimum_score:
                    rejection_counts["relevance"] += 1
                    continue

                metadata_tokens = set(normalize_words(
                    str(hit.get("metadata", "")) + " " + str(hit.get("tags", ""))
                ))
                scene_context = (
                    str(scene.get("visual_query", "")) + " "
                    + str(query or "") + " "
                    + str(scene.get("narration", ""))
                )
                context_conflicts = unrequested_context_conflicts(
                    hit, scene_context, scene.get("narration", "")
                )
                if context_conflicts:
                    rejection_counts["context"] += 1
                    continue

                tags = str(hit.get("tags") or hit.get("metadata") or "")
                similarity = max(
                    [tag_similarity(tags, old) for old in selected_tag_sets] or [0.0]
                )
                if similarity > 0.78:
                    rejection_counts["duplicate"] += 1
                    continue

                # MM7: licence confirmation is independent of visual relevance.
                # An unconfirmed licence ("see file page", "verify terms", empty)
                # is never treated as reuse permission.
                license_info = footage_license.resolve_license(hit)
                if license_required and license_info["status"] != "confirmed":
                    rejection_counts["license"] += 1
                    license_rejections.append({
                        "scene": i,
                        "source": hit.get("source"),
                        "title": str(hit.get("title") or "")[:160],
                        "status": license_info["status"],
                        "evidence": license_info["evidence"],
                    })
                    if len(scene_license_rejections) < 12:
                        scene_license_rejections.append({
                            "source": hit.get("source"),
                            "id": hit.get("id"),
                            "title": hit.get("title"),
                            "page_url": hit.get("page_url"),
                            "status": license_info["status"],
                            "evidence": license_info["evidence"],
                        })
                    continue
                hit["_license_info"] = license_info

                signature = footage_identity_signature(hit)
                if signature and signature in accepted_signatures:
                    rejection_counts["duplicate"] += 1
                    continue

                previous = candidates.get(identity)
                if previous is None or score > previous[0]:
                    candidates[identity] = (score, hit, query, matched, total)

        for query_index, query in enumerate(queries):
            collect_query_candidates(
                query,
                include_coverr=(query_index == 0),
            )

        # Only sparse strict pools pay for relaxed metadata matching and the
        # larger fallback search wave. Relaxed candidates are never auto-accepted:
        # they must pass the same downloaded-video visual verification below.
        ranked = rank_footage_candidates_by_source(candidates.values())
        targeted_search_threshold = min(12, VISION_CANDIDATE_LIMIT)
        if len(ranked) < targeted_search_threshold:
            print(
                f"Scene {i}: strict metadata produced {len(ranked)} candidates; "
                "running a second, AI-gated discovery pass with softer metadata matching."
            )
            for query in list(queries):
                collect_query_candidates(query, allow_relaxed=True)
                ranked = rank_footage_candidates_by_source(candidates.values())
                if len(ranked) >= VISION_CANDIDATE_LIMIT:
                    break

            extra_queries = fallback_visual_queries(
                raw_queries,
                scene.get("narration", ""),
            )
            existing = {query.lower() for query in queries}
            extra_queries = [q for q in extra_queries if q.lower() not in existing]

            if extra_queries and len(ranked) < VISION_CANDIDATE_LIMIT:
                print(
                    f"Scene {i}: trying {len(extra_queries)} additional "
                    "subject-preserving fallback searches."
                )

            for query in extra_queries:
                queries.append(query)
                collect_query_candidates(query, allow_relaxed=True)
                ranked = rank_footage_candidates_by_source(candidates.values())
                if len(ranked) >= VISION_CANDIDATE_LIMIT:
                    break

        # A candidate is queued only when no admitted candidate already covers
        # it, and it is still subject to the same downloaded-video visual review.
        admitted_identities = {
            f"{item[1].get('source')}:{item[1].get('id')}" for item in ranked
        }
        vision_queue = [
            (hit, query, score, matched, total)
            for identity, (score, matched, total, hit, query) in pending_vision.items()
            if identity not in admitted_identities
        ]
        vision_queue.sort(key=lambda item: -item[2])

        print(
            f"Scene {i}: footage discovery summary — "
            f"strict={strict_candidate_count}, relaxed={relaxed_candidate_count}, "
            f"ranked={len(ranked)}, partial-metadata-for-visual-review={len(vision_queue)}, "
            f"visual-check-limit={VISION_CANDIDATE_LIMIT}; "
            "filtered=" + ", ".join(
                f"{key}:{value}" for key, value in rejection_counts.items() if value
            )
        )

        # Semantic relevance is the primary sort signal. Trusted open archives
        # only break close-score ties; a much stronger match can rank first.
        ranked = rank_footage_candidates_by_source(candidates.values())
        portrait_hints = sum(
            1 for item in ranked if has_native_portrait_metadata_hint(item[1])
        )
        deferred_orientation = sum(
            1 for item in ranked
            if item[1].get("width") and item[1].get("height")
            and not has_native_portrait_metadata_hint(item[1])
        )
        print(
            f"Scene {i}: portrait metadata hints={portrait_hints}; "
            f"orientation deferred to downloaded-file ffprobe={deferred_orientation}."
        )

        print(
            f"Scene {i}: {len(candidates)} metadata-qualified "
            f"candidate(s) across {len(queries)} search variants; "
            f"checking up to "
            f"{min(len(ranked), VISION_CANDIDATE_LIMIT)} candidates."
        )

        for preview_index, preview_item in enumerate(
            ranked[:5],
            1
        ):
            (
                preview_score,
                preview_hit,
                preview_query,
                preview_matched,
                preview_total
            ) = preview_item

            print(
                f"  Candidate rank {preview_index}: "
                f"{preview_hit.get('source')} "
                f"ID={preview_hit.get('id')} "
                f"score={preview_score:.2f} "
                f"match={preview_matched}/{preview_total} "
                f"query={preview_query!r} "
                f"metadata={str(preview_hit.get('metadata') or preview_hit.get('tags') or '')[:160]}"
            )

        destination = FOOT / f"scene_{i:02d}.mp4"

        download_errors = []
        selected = None
        checked = 0

        candidate_attempts = list(ranked[:VISION_CANDIDATE_LIMIT])
        # Partial-metadata candidates are appended last so they can never displace
        # a candidate that passed the metadata anchors, but they are still shown
        # to the visual model instead of being discarded unread.
        partial_budget = max(0, VISION_CANDIDATE_LIMIT // 3)
        for hit, query, score, matched, total in vision_queue[:partial_budget]:
            candidate_attempts.append((score, hit, query, matched, total))
        attempted_candidate_ids = set()
        attempt_cursor = 0
        adaptive_search_done = False
        adaptive_retry_queries_used = []

        while True:
            if attempt_cursor >= len(candidate_attempts):
                if not adaptive_search_done and selected is None:
                    adaptive_search_done = True
                    retry_queries = adaptive_visual_retry_queries(
                        scene, visual_rejection_feedback
                    )
                    existing_queries = {value.lower() for value in queries}
                    retry_queries = [
                        value for value in retry_queries
                        if value.lower() not in existing_queries
                    ]

                    if retry_queries:
                        print(
                            f"Scene {i}: initial candidates exhausted; starting "
                            f"one rejection-guided search retry with "
                            f"{len(retry_queries)} targeted query variant(s)."
                        )
                    for retry_index, retry_query in enumerate(retry_queries):
                        queries.append(retry_query)
                        adaptive_retry_queries_used.append(retry_query)
                        collect_query_candidates(
                            retry_query,
                            allow_relaxed=True,
                            include_coverr=(retry_index == 0),
                        )

                    if retry_queries:
                        ranked = rank_footage_candidates_by_source(candidates.values())
                        new_candidates = [
                            item for item in ranked
                            if f"{item[1].get('source')}:{item[1].get('id')}"
                            not in attempted_candidate_ids
                        ]
                        retry_batch = new_candidates[:MAX_ADAPTIVE_FOOTAGE_CANDIDATES]
                        if retry_batch:
                            print(
                                f"Scene {i}: adaptive retry found "
                                f"{len(new_candidates)} untried candidate(s); "
                                f"checking up to {len(retry_batch)} of them."
                            )
                            candidate_attempts.extend(retry_batch)
                            continue
                        print(f"Scene {i}: adaptive retry added no new eligible candidates.")
                break

            score, hit, query, matched, total = candidate_attempts[attempt_cursor]
            attempt_cursor += 1
            identity = f"{hit.get('source')}:{hit.get('id')}"
            if identity in attempted_candidate_ids:
                continue
            attempted_candidate_ids.add(identity)

            candidate_source = str(hit.get("source") or "").strip()
            cooldown_remaining = footage_source_cooldown_remaining(candidate_source)
            if cooldown_remaining > 0:
                remaining = max(1, int(cooldown_remaining))
                print(
                    f"Scene {i}: skipping remaining {candidate_source} "
                    f"candidates during its {remaining}s rate-limit cooldown."
                )
                continue

            license_info = hit.get("_license_info") or footage_license.resolve_license(hit)
            if license_required and license_info["status"] != "confirmed":
                print(
                    f"Scene {i}: candidate skipped — reuse licence is "
                    f"{license_info['status']} ({license_info['evidence'][:120]})."
                )
                if len(scene_license_rejections) < 12:
                    scene_license_rejections.append({
                        "source": hit.get("source"),
                        "id": hit.get("id"),
                        "title": hit.get("title"),
                        "page_url": hit.get("page_url"),
                        "status": license_info["status"],
                        "evidence": license_info["evidence"],
                    })
                continue
            hit["_license_info"] = license_info

            checked += 1

            try:
                download_limit = candidate_download_limit(score, hit)
                print(
                    f"Scene {i}: candidate download budget="
                    f"{download_limit / 1024 / 1024:.0f} MB "
                    f"(metadata relevance {score:.2f})."
                )
                download_stream(
                    hit["url"],
                    destination,
                    source=candidate_source,
                    max_bytes=download_limit,
                )

                source_video_info = ffprobe_video_info(destination)
                native_portrait = is_native_portrait_916(
                    source_video_info["width"], source_video_info["height"],
                    aspect_ratio=source_video_info["aspect_ratio"],
                )
                requires_smart_crop = not native_portrait
                if requires_smart_crop and not is_smart_reframe_eligible(
                    source_video_info["width"], source_video_info["height"],
                    source_video_info["aspect_ratio"],
                ):
                    raise RuntimeError(
                        "Downloaded source is neither native 9:16 nor a supported landscape "
                        "source for content-aware reframing: "
                        f"{source_video_info['width']}x{source_video_info['height']} "
                        f"(aspect {source_video_info['aspect_ratio']:.4f})."
                    )
                hit["_source_video_info"] = source_video_info

                if visual_provider_names:
                    candidate_metadata = " | ".join([
                        f"TITLE: {hit.get('title', '')}",
                        f"TAGS: {hit.get('tags', '')}",
                        f"DESCRIPTION: {hit.get('metadata', '')}",
                    ])

                    (
                        visually_relevant,
                        confidence,
                        visual_reason,
                        vision_provider
                    ) = verify_footage_visually(
                        destination,
                        scene,
                        query,
                        candidate_metadata
                    )

                    print(
                        f"Scene {i}: visual check {checked}/"
                        f"{min(len(ranked), VISION_CANDIDATE_LIMIT)} "
                        f"via {vision_provider} — "
                        f"{'PASS' if visually_relevant else 'REJECT'} "
                        f"({confidence:.2f}): "
                        f"{visual_reason[:220]}"
                    )

                    if not visually_relevant:
                        scene_vision_rejections.append({
                            "source": hit.get("source"),
                            "id": hit.get("id"),
                            "title": hit.get("title"),
                            "query": query,
                            "status": "rejected",
                            "provider": vision_provider,
                            "confidence": round(float(confidence), 3),
                            "reason": str(visual_reason)[:500],
                        })
                        visual_rejection_feedback.append({
                            "reason": visual_reason,
                            "query": query,
                            "metadata": candidate_metadata[:900],
                        })
                        rejected_context_groups.update(
                            visual_rejection_context_groups(
                                [visual_rejection_feedback[-1]],
                                scene.get("visual_query", ""),
                                scene.get("narration", ""),
                            )
                        )
                        destination.unlink(missing_ok=True)
                        continue

                else:
                    confidence = None
                    vision_provider = "Metadata-only"
                    visual_reason = (
                        "Metadata-only fallback explicitly enabled."
                    )

                if requires_smart_crop:
                    if not visual_provider_names:
                        raise RuntimeError(
                            "Landscape footage cannot be reframed without AI crop tracking; "
                            "native 9:16 footage is still required in metadata-only mode."
                        )
                    crop_plan = analyze_smart_crop(
                        destination,
                        scene,
                        query,
                        candidate_metadata,
                        source_video_info,
                    )
                    cropped_frames = extract_smart_crop_verification_frames(
                        destination,
                        source_video_info,
                        crop_plan,
                    )
                    (
                        crop_relevant,
                        crop_confidence,
                        crop_reason,
                        crop_provider,
                    ) = verify_footage_visually(
                        destination,
                        scene,
                        query,
                        candidate_metadata + " | Proposed dynamic crop: " + crop_plan.get("subject", ""),
                        frames_override=cropped_frames,
                    )
                    print(
                        f"Scene {i}: post-crop verification via {crop_provider} — "
                        f"{'PASS' if crop_relevant else 'REJECT'} "
                        f"({crop_confidence:.2f}): {crop_reason[:200]}"
                    )
                    if not crop_relevant:
                        visual_rejection_feedback.append({
                            "reason": "Post-crop verification: " + crop_reason,
                            "query": query,
                            "metadata": candidate_metadata[:900],
                        })
                        destination.unlink(missing_ok=True)
                        continue
                    hit["_smart_crop_plan"] = crop_plan
                    visual_reason += (
                        f" | Smart 9:16 crop verified via {crop_provider} "
                        f"({crop_confidence:.2f}): {crop_reason[:180]}"
                    )
                    vision_provider = f"{vision_provider} + crop tracker {crop_plan.get('vision_provider', '')}"
                    confidence = min(
                        value for value in (confidence, crop_confidence) if value is not None
                    )

                selected = (
                    score,
                    hit,
                    query,
                    matched,
                    total,
                    confidence,
                    visual_reason,
                    vision_provider,
                )

                break

            except Exception as exc:
                message = str(exc)

                download_errors.append(
                    f"{hit.get('source')} "
                    f"{hit.get('id')}: {message}"
                )
                scene_vision_rejections.append({
                    "source": hit.get("source"),
                    "id": hit.get("id"),
                    "title": hit.get("title"),
                    "query": query,
                    "status": "failed",
                    "provider": "n/a",
                    "confidence": None,
                    "reason": message[:500],
                })

                print(
                    f"Scene {i}: candidate rejected/skipped "
                    f"({hit.get('source')}): {message[:350]}"
                )

                destination.unlink(missing_ok=True)
                destination.with_name(destination.name + ".part").unlink(missing_ok=True)

                if isinstance(exc, VisionVerificationError):
                    raise RuntimeError(
                        "Visual verification cannot continue after automatic "
                        "provider failover. " + message
                    ) from exc

        reused_from_scene = None

        if selected is None and verified_assets:
            print(
                f"Scene {i}: no unique candidate passed; checking previously "
                "verified footage as a last-resort related-scene fallback."
            )

            for previous_asset in verified_assets:
                previous_hit = previous_asset["hit"]
                previous_path = Path(previous_asset["path"])
                if not previous_path.is_file():
                    continue

                for fallback_query in queries:
                    if not candidate_matches_visual_anchors(
                        previous_hit, fallback_query,
                        primary_query=scene.get("visual_query"),
                    ):
                        continue

                    reuse_score, reuse_matched, reuse_total = relevance_score(
                        previous_hit,
                        fallback_query,
                        scene.get("narration", ""),
                    )
                    if reuse_matched < 1 or reuse_score < 0.28:
                        continue

                    candidate_metadata = " | ".join([
                        str(previous_hit.get("metadata", "")),
                        str(previous_hit.get("tags", "")),
                    ])

                    try:
                        if visual_provider_names:
                            (
                                reuse_relevant,
                                reuse_confidence,
                                reuse_reason,
                                reuse_provider,
                            ) = verify_footage_visually(
                                previous_path,
                                scene,
                                fallback_query,
                                candidate_metadata,
                            )
                            print(
                                f"Scene {i}: reuse check of Scene "
                                f"{previous_asset['scene']} via {reuse_provider} — "
                                f"{'PASS' if reuse_relevant else 'REJECT'} "
                                f"({reuse_confidence:.2f}): {reuse_reason[:180]}"
                            )
                        else:
                            reuse_relevant = True
                            reuse_confidence = None
                            reuse_provider = "Metadata-only"
                            reuse_reason = (
                                "Previously accepted clip; metadata-only reuse "
                                "is permitted by the explicit configuration."
                            )

                    except VisionVerificationError as exc:
                        raise RuntimeError(
                            "Visual verification cannot continue during the "
                            "related-scene fallback. " + str(exc)
                        ) from exc

                    if not reuse_relevant:
                        continue

                    shutil.copyfile(previous_path, destination)
                    selected = (
                        reuse_score,
                        previous_hit,
                        fallback_query,
                        reuse_matched,
                        reuse_total,
                        reuse_confidence,
                        reuse_reason,
                        reuse_provider,
                    )
                    reused_from_scene = previous_asset["scene"]
                    print(
                        f"Scene {i}: reusing the already verified clip from "
                        f"Scene {reused_from_scene}; visual re-check passed."
                    )
                    break

                if selected is not None:
                    break

        if selected is None:
            if not ranked:
                reason = (
                    "No candidate passed the metadata relevance filter."
                )

            elif checked >= min(
                len(ranked),
                VISION_CANDIDATE_LIMIT
            ):
                reason = (
                    f"None of the {checked} candidates checked from "
                    f"{len(ranked)} metadata-ranked candidates passed "
                    "visual verification or downloaded successfully."
                )

            else:
                reason = (
                    "No candidate could be downloaded and visually verified."
                )

            if download_errors:
                reason += (
                    " Details: "
                    + " | ".join(download_errors[:4])
                )

            raise RuntimeError(
                f"Scene {i} could not obtain verified relevant footage. "
                f"{reason} Queries tried: {queries}. "
                "No unrelated footage or diagram will be substituted."
            )

        (
            score,
            hit,
            query,
            matched,
            total,
            vision_confidence,
            vision_reason,
            vision_provider,
        ) = selected

        identity = (
            f"{hit.get('source')}:{hit.get('id')}"
        )

        used_ids.add(identity)

        selected_tag_sets.append(
            str(
                hit.get("tags")
                or hit.get("metadata")
                or ""
            )
        )

        if reused_from_scene is None:
            verified_assets.append({
                "scene": i,
                "hit": hit,
                "path": str(destination),
            })
        else:
            scene["reused_footage_from_scene"] = reused_from_scene

        scene["media_file"] = str(destination)
        scene["visual_type"] = "footage"
        scene["selected_footage_source"] = hit.get("source")
        scene["selected_footage_tags"] = (
            hit.get("tags")
            or hit.get("metadata", "")
        )
        scene["selected_footage_url"] = hit.get(
            "page_url",
            ""
        )
        # Asset provenance for credits.txt / copyright_report.json.
        scene["source"] = hit.get("source")
        scene["creator"] = hit.get("creator")
        scene["page_url"] = hit.get("page_url", "")
        scene["license"] = hit.get("license", "")
        license_info = hit.get("_license_info") or footage_license.resolve_license(hit)
        scene["license_info"] = license_info
        scene["license_reuse_confirmed"] = bool(license_info.get("reuse_allowed"))
        accepted_signatures.add(footage_identity_signature(hit))
        scene["coverr_attribution"] = str(hit.get("source") or "") == "Coverr"
        scene["source_video_dimensions"] = hit.get("_source_video_info", {})
        scene["output_aspect_ratio"] = "9:16"
        smart_crop_plan = hit.get("_smart_crop_plan")
        if smart_crop_plan:
            scene["smart_vertical_reframe"] = smart_crop_plan
        else:
            scene["smart_vertical_reframe"] = {"enabled": False, "reason": "Native 9:16 source."}

        scene["footage_search_retry_count"] = len(adaptive_retry_queries_used)
        scene["footage_search_retry_queries"] = list(adaptive_retry_queries_used)
        scene["footage_visual_rejection_count"] = len(visual_rejection_feedback)
        scene["vision_rejections"] = list(scene_vision_rejections)
        scene["license_rejections"] = list(scene_license_rejections)

        # Keep the exact frames the vision model judged, as audit evidence.
        evidence_dir = OUT / "vision" / f"scene_{i:02d}"
        evidence_frames = []
        if vision_confidence is not None:
            for stale in evidence_dir.glob("*.jpg"):
                stale.unlink(missing_ok=True)
            try:
                extract_vision_frames(destination, frame_count=3, save_dir=evidence_dir)
                evidence_frames = sorted(str(path) for path in evidence_dir.glob("*.jpg"))
            except Exception as exc:
                print(f"Scene {i}: could not store verification frames: {str(exc)[:200]}")

        scene["visual_verification"] = {
            "status": "approved" if vision_confidence is not None else "unverified",
            "method": (
                f"{vision_provider} Vision"
                if vision_confidence is not None
                else "metadata-only fallback (no vision provider configured)"
            ),
            "provider": vision_provider,
            "confidence": vision_confidence,
            "reason": vision_reason,
            "query": query,
            "frames": evidence_frames,
            "frame_count": len(evidence_frames),
            "rejected_candidates": len(scene_vision_rejections),
        }
        if vision_confidence is None:
            print(
                f"WARNING: Scene {i} footage is UNVERIFIED — no AI Vision provider "
                "inspected the downloaded clip. It must not be reported as approved."
            )

        footage_audit.append({
            "scene": i,
            "narration": scene.get("narration", ""),
            "visual_query": scene.get("visual_query", ""),
            "accepted": {
                "source": hit.get("source"),
                "id": hit.get("id"),
                "title": hit.get("title"),
                "page_url": hit.get("page_url", ""),
                "file_url": hit.get("url"),
                "media_file": str(destination),
                "license": license_info,
            },
            "verification": scene["visual_verification"],
            "rejected_candidates": list(scene_vision_rejections),
            "license_rejections": list(scene_license_rejections),
            "queries_tried": list(queries),
            "reused_from_scene": reused_from_scene,
        })

        vision_credit = (
            f" | Visual verification: "
            f"{vision_provider} {vision_confidence:.2f} — "
            f"{vision_reason}"
            if vision_confidence is not None
            else (
                " | Visual verification: disabled "
                "(metadata-only fallback)"
            )
        )

        coverr_attribution = ""
        if str(hit.get("source") or "") == "Coverr":
            coverr_creator_url = str(hit.get("creator_url") or "").strip()
            coverr_attribution = (
                " | Coverr API Demo attribution: "
                f"Video by {hit.get('creator', 'Coverr creator')} on Coverr; "
                "Coverr https://coverr.co"
                + (
                    f" | Creator profile: {coverr_creator_url}"
                    if coverr_creator_url.startswith("https://")
                    else f" | Creator/video source: {hit.get('page_url', '')}"
                )
            )

        credits.append(
            f"Scene {i} | {hit.get('source')} | "
            f"Creator: {hit.get('creator', 'Unknown')} | "
            f"Source: {hit.get('page_url', '')} | "
            f"Metadata relevance: {score:.2f} "
            f"({matched}/{total} query concepts)"
            f"{vision_credit} | "
            f"License: {hit.get('license', 'Check source page')}"
            f"{coverr_attribution}"
            + (
                f" | Last-resort reuse of previously verified Scene {reused_from_scene}"
                if reused_from_scene is not None
                else ""
            )
        )

        print(
            f"Scene {i}: accepted {hit.get('source')} "
            f"metadata={score:.2f}, "
            f"visual confidence="
            f"{vision_confidence if vision_confidence is not None else 'not checked'} — "
            f"{str(hit.get('metadata', ''))[:140]}"
        )

    Path("credits.txt").write_text(
        "\n".join(credits) + "\n",
        encoding="utf-8"
    )

    unverified_scenes = [
        entry["scene"] for entry in footage_audit
        if entry["verification"].get("status") != "approved"
    ]
    verification_payload = {
        "schema": "mm7-footage-verification",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vision_required": VISION_REQUIRED,
        "vision_providers": visual_provider_names,
        "license_required": license_required,
        "scene_count": len(footage_audit),
        "approved_scenes": len(footage_audit) - len(unverified_scenes),
        "unverified_scenes": unverified_scenes,
        "scenes": footage_audit,
        "license_rejections": license_rejections[:80],
    }
    Path("footage_verification.json").write_text(
        json.dumps(verification_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    PIPELINE_STATE["footage_verification"] = "footage_verification.json"
    PIPELINE_STATE["unverified_scenes"] = unverified_scenes
    print(
        f"Footage verification report: {verification_payload['approved_scenes']}/"
        f"{verification_payload['scene_count']} approved by AI Vision, "
        f"{len(license_rejections)} candidate(s) rejected for an unconfirmed licence."
    )

    Path("plan.json").write_text(
        json.dumps(
            plan,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# VOICEOVER AND SUBTITLES
# ============================================================

def ass_time(seconds):
    ticks = max(
        0,
        int(round(seconds * 100))
    )

    return (
        f"{ticks // 360000}:"
        f"{(ticks // 6000) % 60:02}:"
        f"{(ticks // 100) % 60:02}."
        f"{ticks % 100:02}"
    )



def build_caption_groups(events, max_words=CAPTION_MAX_WORDS, max_chars=CAPTION_MAX_CHARS):
    """Chunk timed words from one sentence; never join adjacent sentences."""
    groups = []
    current = []
    for event in events:
        word = str(event.get("text") or "").strip()
        if not word:
            continue
        candidate_chars = sum(len(str(item.get("text") or "")) for item in current)
        candidate_chars += len(word) + max(0, len(current))
        if current and (len(current) >= max_words or candidate_chars > max_chars):
            groups.append(current)
            current = []
        current.append(event)
    if current:
        groups.append(current)

    # Avoid a dangling one-word caption when the previous phrase can be split evenly.
    if len(groups) > 1 and len(groups[-1]) == 1 and len(groups[-2]) > 3:
        groups[-1].insert(0, groups[-2].pop())
    return groups


def wrap_overlay_text(value, width=24, max_lines=4):
    """Wrap an opening hook without dropping any text at the line limit."""
    clean = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return ""

    line_width = max(1, int(width))
    max_lines = max(1, int(max_lines))
    while True:
        wrapped = textwrap.wrap(
            clean,
            width=line_width,
            break_long_words=True,
            break_on_hyphens=False,
        )
        if len(wrapped) <= max_lines:
            break
        # Increase width in small steps until every character fits. This is
        # preferable to silently slicing off the final line of a long hook.
        line_width += max(4, int(len(clean) / max_lines / 8) or 4)
    return "\n".join(wrapped)

def _create_silence(path, duration):
    duration = float(duration)
    if duration <= 0.001:
        return None
    run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "anullsrc=r=48000:cl=stereo",
        "-t", f"{duration:.4f}",
        "-c:a", "pcm_s16le",
        str(path),
    ])
    return Path(path)


def _speechify_choose_voice(api_keys):
    """Discover the best calm/deep male voice, rotating catalog keys and pages."""
    if SPEECHIFY_VOICE_ID:
        return SPEECHIFY_VOICE_ID

    if isinstance(api_keys, str):
        api_keys = [api_keys]
    api_keys = [str(key).strip() for key in (api_keys or []) if str(key).strip()]
    url = "https://api.speechify.ai/v1/voices"
    preferred = (SPEECHIFY_DOCUMENTED_VOICE_ID, "george", "henry")

    for key_index, api_key in enumerate(api_keys):
        voices = []
        cursor = None
        seen_cursors = set()
        try:
            # The catalog is cursor-paginated. Read enough pages to avoid
            # selecting a random early result when a better-fit voice is later.
            for page_index in range(8):
                params = {
                    "type": "shared",
                    "locale": LANGUAGE["speechify_locale"],
                    "gender": "male",
                    "model": SPEECHIFY_MODEL,
                    "limit": 200,
                }
                if cursor:
                    params["cursor"] = cursor
                response = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    params=params,
                    timeout=(12, 35),
                )
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"Speechify voice catalog HTTP {response.status_code}: {response.text[:250]}"
                    )
                data = response.json()
                page_voices = data.get("voices", data.get("data", []))
                if isinstance(page_voices, dict):
                    page_voices = page_voices.get("voices", [])
                if isinstance(page_voices, list):
                    voices.extend(v for v in page_voices if isinstance(v, dict))

                next_cursor = data.get("next_cursor") or data.get("nextCursor")
                has_more = bool(data.get("has_more", data.get("hasMore", False)))
                if not has_more or not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor

            scored = []
            for voice in voices:
                voice_id = str(voice.get("id") or voice.get("voice_id") or "").strip()
                if not voice_id:
                    continue
                tags = voice.get("tags", [])
                if isinstance(tags, list):
                    tags_text = " ".join(str(tag) for tag in tags)
                else:
                    tags_text = str(tags or "")
                descriptor = " ".join([
                    str(voice.get("display_name", "")),
                    str(voice.get("name", "")),
                    str(voice.get("description", "")),
                    tags_text,
                    voice_id,
                ]).lower()
                score = 0
                for term, points in (
                    ("calm", 5), ("deep", 5), ("baritone", 6),
                    ("warm", 2), ("narration", 3), ("documentary", 4),
                    ("authoritative", 2), ("grounded", 2), ("low-pitch", 3),
                ):
                    if term in descriptor:
                        score += points
                staff_rank = voice.get("staff_pick_rank")
                try:
                    staff_bonus = max(0, 3 - int(staff_rank)) if staff_rank is not None else 0
                except (TypeError, ValueError):
                    staff_bonus = 0
                scored.append((score + staff_bonus, voice_id))

            if scored:
                scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
                best_score, best_id = scored[0]
                if best_score > 0:
                    print(
                        f"Speechify selected calm/deep voice profile: {best_id} "
                        f"(catalog key slot {key_index + 1})."
                    )
                    return best_id
                for preferred_id in preferred:
                    if any(voice_id.lower() == preferred_id for _, voice_id in scored):
                        return preferred_id
        except Exception as exc:
            print(
                f"Speechify voice catalog key slot {key_index + 1} unavailable; "
                f"trying the next configured key: {str(exc)[:220]}"
            )
            continue

    # The catalog is unreadable with this credential. Returning an empty string
    # lets the caller fall through to the verified documented voice instead of
    # guessing an id the speech endpoint may reject.
    print(
        "Speechify voice catalog is not readable with the configured credential "
        "(403 insufficient_scope is a token scope limit, not a bad key). Falling "
        "back to the documented shared voice id."
    )
    return ""


def _speechify_voice_candidates(api_keys):
    """Ordered voice ids: pinned secret, catalog pick, documented shared voice."""
    candidates = []
    for candidate in (SPEECHIFY_VOICE_ID, _speechify_choose_voice(api_keys), SPEECHIFY_DOCUMENTED_VOICE_ID):
        candidate = str(candidate or "").strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _speechify_invalid_voice_error(error):
    """True when the error means this voice id is unusable, not the account."""
    match = re.search(r"HTTP (\d{3})", str(error or ""))
    return bool(match and match.group(1) in ("400", "404", "422"))


def _speechify_sentence_ssml(sentence, index, total):
    """Create calm, lower-pitched SSML with gentle variation for narrative emphasis."""
    escaped = html.escape(sentence, quote=False)
    if index == 0:
        rate, pitch = "-3%", "-4%"  # restrained hook, still clear
    elif sentence.rstrip().endswith("?"):
        rate, pitch = "-3%", "-3%"  # lift a question slightly
    elif index == total - 1:
        rate, pitch = "-7%", "-10%"  # grounded final thought
    else:
        rate, pitch = "-6%", "-8%"  # calm, deep documentary baseline
    return (
        "<speak><prosody "
        f'rate="{rate}" pitch="{pitch}"'
        f">{escaped}</prosody></speak>"
    )


def _flatten_speechify_word_marks(value):
    marks = []
    def walk(item):
        if isinstance(item, dict):
            has_timing = (
                item.get("start_time") is not None
                and item.get("end_time") is not None
                and (item.get("value") is not None or item.get("text") is not None)
            )
            if has_timing and str(item.get("type", "word")).lower() in ("word", "token", ""):
                try:
                    start_ms = float(item["start_time"])
                    end_ms = float(item["end_time"])
                    word = str(item.get("value", item.get("text", ""))).strip()
                    if word and end_ms >= start_ms:
                        marks.append({
                            "text": word,
                            "start": max(0.0, start_ms / 1000.0),
                            "duration": max(0.01, (end_ms - start_ms) / 1000.0),
                        })
                except (TypeError, ValueError):
                    pass
            for child in item.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    walk(value)
    marks.sort(key=lambda item: item["start"])
    # Avoid accidental duplicates where an API wraps the same word-mark object twice.
    unique = []
    seen = set()
    for mark in marks:
        identity = (mark["text"], round(mark["start"], 4), round(mark["duration"], 4))
        if identity not in seen:
            seen.add(identity)
            unique.append(mark)
    return unique


def _fallback_sentence_word_marks(sentence, duration):
    words = re.findall(r"\S+", sentence)
    if not words:
        return []
    weights = [max(1, len(re.sub(r"\W", "", word))) for word in words]
    total_weight = float(sum(weights))
    cursor = 0.0
    marks = []
    for word, weight in zip(words, weights):
        span = max(0.035, duration * weight / total_weight)
        marks.append({"text": word, "start": cursor, "duration": span})
        cursor += span
    if marks:
        marks[-1]["duration"] = max(0.035, duration - marks[-1]["start"])
    return marks


def _request_speechify_audio(sentence, sentence_index, sentence_total, voice_id, api_keys, active_key_index):
    """Try the current Speechify key, then rotate through remaining keys on quota/auth failures."""
    body = {
        "input": _speechify_sentence_ssml(sentence, sentence_index, sentence_total),
        "voice_id": voice_id,
        "audio_format": "wav",
        "language": LANGUAGE["speechify_locale"],
        "model": SPEECHIFY_MODEL,
    }
    last_error = None
    key_order = list(range(active_key_index, len(api_keys))) + list(range(0, active_key_index))
    tried = set()
    for key_index in key_order:
        if key_index in tried:
            continue
        tried.add(key_index)
        response = requests.post(
            "https://api.speechify.ai/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {api_keys[key_index]}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=(20, 120),
        )
        _observe_provider_response("Speechify", response, api_key=api_keys[key_index])
        if response.status_code >= 400:
            detail = response.text[:400].replace("\n", " ")
            last_error = RuntimeError(
                f"Speechify key {key_index + 1} HTTP {response.status_code}: {detail}"
            )
            if response.status_code in (402, 429):
                print(
                    f"Speechify account quota/rate limit reached (HTTP {response.status_code}); "
                    "all keys on this account share the same budget. Switching to the VO fallback."
                )
                raise last_error
            if response.status_code in (401, 403) or response.status_code >= 500:
                print(
                    f"Speechify key slot {key_index + 1} unavailable "
                    f"(HTTP {response.status_code}); checking whether another credential works."
                )
                continue
            raise last_error
        try:
            data = response.json()
            audio_data = data.get("audio_data") or data.get("audio")
            if not audio_data:
                raise ValueError("Speechify response contains no audio_data.")
            if isinstance(audio_data, str) and ",base64," in audio_data:
                audio_data = audio_data.split(",base64,", 1)[1]
            audio_bytes = base64.b64decode(audio_data)
            marks = _flatten_speechify_word_marks(data.get("speech_marks", []))
            return audio_bytes, marks, key_index
        except Exception as exc:
            raise RuntimeError(f"Could not decode Speechify audio response: {exc}") from exc
    raise RuntimeError(f"All configured Speechify API keys failed: {last_error}")


def _generate_speechify_parts(sentences):
    global SPEECHIFY_VOICE_USED
    api_keys = SPEECHIFY_KEYS
    if not api_keys:
        raise RuntimeError("SPVO_API/SPEECHIFY_API_KEY is not configured.")
    voice_candidates = _speechify_voice_candidates(api_keys)
    if not voice_candidates:
        raise RuntimeError("Speechify has no usable voice id (no pinned, catalog or documented voice).")
    voice_id = voice_candidates[0]
    SPEECHIFY_VOICE_USED = voice_id
    wav_parts = []
    sentence_word_events = []
    sentence_events = []
    cursor = 0.0
    key_index = 0

    opening = _create_silence(OUT / "opening_silence.wav", OPENING_LEAD)
    if opening:
        wav_parts.append(opening)
        cursor += ffprobe_duration(opening)

    for index, sentence in enumerate(sentences):
        if len(sentence) > 1950:
            raise RuntimeError(
                f"Speechify sentence {index + 1} exceeds the safe 1,950-character request budget."
            )
        if index == 0 and len(voice_candidates) > 1:
            # The first sentence proves the voice id. A rejected id rotates to
            # the next candidate instead of aborting the whole narration.
            last_voice_error = None
            for candidate in voice_candidates:
                try:
                    audio_bytes, word_marks, key_index = _request_speechify_audio(
                        sentence, index, len(sentences), candidate, api_keys, key_index
                    )
                except RuntimeError as exc:
                    if not _speechify_invalid_voice_error(exc):
                        raise
                    print(
                        f"Speechify rejected voice {candidate!r}; trying the next "
                        "configured voice id."
                    )
                    last_voice_error = exc
                    continue
                if candidate != voice_id:
                    print(f"Speechify switched to voice {candidate!r} after a voice-id rejection.")
                voice_id = candidate
                SPEECHIFY_VOICE_USED = candidate
                break
            else:
                raise last_voice_error or RuntimeError("Every configured Speechify voice id was rejected.")
        else:
            audio_bytes, word_marks, key_index = _request_speechify_audio(
                sentence, index, len(sentences), voice_id, api_keys, key_index
            )
        raw_path = OUT / f"tts_{index:02d}_speechify_raw.wav"
        wav_path = OUT / f"tts_{index:02d}.wav"
        raw_path.write_bytes(audio_bytes)
        run([
            "ffmpeg", "-y", "-i", str(raw_path),
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(wav_path),
        ])
        duration = ffprobe_duration(wav_path)
        if duration <= 0.05:
            raise RuntimeError(f"Speechify returned an empty audio segment for sentence {index + 1}.")
        sentence_start = cursor
        local_marks = word_marks or _fallback_sentence_word_marks(sentence, duration)
        timed_sentence_words = []
        for mark in local_marks:
            local_start = min(max(0.0, mark["start"]), max(0.0, duration - 0.02))
            local_duration = min(max(0.035, mark["duration"]), max(0.035, duration - local_start))
            timed = {
                "text": mark["text"],
                "offset": cursor + local_start,
                "duration": local_duration,
            }
            timed_sentence_words.append(timed)
        terminal = re.search(r"([.!?][\"']?)$", sentence.strip())
        if timed_sentence_words and terminal:
            last_text = str(timed_sentence_words[-1].get("text") or "")
            if not re.search(r"[.!?][\"']?$", last_text):
                timed_sentence_words[-1]["text"] = last_text + terminal.group(1)
        sentence_word_events.append(timed_sentence_words)
        wav_parts.append(wav_path)
        cursor += duration
        sentence_events.append({"start": sentence_start, "end": cursor})
        if index < len(sentences) - 1:
            gap = HOOK_GAP if index == 0 else SENTENCE_GAP
            silence = _create_silence(OUT / f"gap_{index:02d}.wav", gap)
            if silence:
                wav_parts.append(silence)
                cursor += ffprobe_duration(silence)
        print(f"Speechify VO sentence {index + 1}/{len(sentences)} rendered ({duration:.2f}s).")

    return sentence_word_events, sentence_events, wav_parts


def _generate_edge_parts(sentences):
    """Sentence-level Edge TTS fallback, retained for resilience."""
    async def generate():
        sentence_word_events = []
        sentence_events = []
        wav_parts = []
        cursor = 0.0
        opening = _create_silence(OUT / "opening_silence.wav", OPENING_LEAD)
        if opening:
            wav_parts.append(opening)
            cursor += ffprobe_duration(opening)
        for index, sentence in enumerate(sentences):
            rate_values = [BASE_RATE, "-5%", "-3%", "-4%"]
            if VO_RATE_OVERRIDE:
                # Duration-recovery retry: a natural, uniformly slower delivery.
                rate_values = [VO_RATE_OVERRIDE] * len(rate_values)
            pitch_values = [BASE_PITCH, "-2Hz", "-4Hz", "-2Hz"]
            mp3_path = OUT / f"tts_{index:02d}.mp3"
            wav_path = OUT / f"tts_{index:02d}.wav"
            boundaries = []
            communicate = edge_tts.Communicate(
                sentence,
                VOICE,
                rate=rate_values[index % len(rate_values)],
                pitch=pitch_values[index % len(pitch_values)],
                boundary="WordBoundary",
            )
            with open(mp3_path, "wb") as audio_file:
                async for message in communicate.stream():
                    if message["type"] == "audio":
                        audio_file.write(message["data"])
                    elif message["type"] == "WordBoundary":
                        boundaries.append({
                            "text": message["text"],
                            "offset": message["offset"] / 10_000_000,
                            "duration": message["duration"] / 10_000_000,
                        })
            if not mp3_path.exists() or mp3_path.stat().st_size < 1000 or not boundaries:
                raise RuntimeError(f"Edge TTS failed on sentence {index + 1}.")
            run([
                "ffmpeg", "-y", "-i", str(mp3_path),
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(wav_path),
            ])
            duration = ffprobe_duration(wav_path)
            sentence_start = cursor
            timed_sentence_words = []
            for event in boundaries:
                timed_sentence_words.append({
                    **event,
                    "offset": cursor + event["offset"],
                })
            terminal = re.search(r"([.!?][\"']?)$", sentence.strip())
            if timed_sentence_words and terminal:
                last_text = str(timed_sentence_words[-1].get("text") or "")
                if not re.search(r"[.!?][\"']?$", last_text):
                    timed_sentence_words[-1]["text"] = last_text + terminal.group(1)
            sentence_word_events.append(timed_sentence_words)
            wav_parts.append(wav_path)
            cursor += duration
            sentence_events.append({"start": sentence_start, "end": cursor})
            if index < len(sentences) - 1:
                gap = HOOK_GAP if index == 0 else SENTENCE_GAP
                silence = _create_silence(OUT / f"gap_{index:02d}.wav", gap)
                if silence:
                    wav_parts.append(silence)
                    cursor += ffprobe_duration(silence)
        return sentence_word_events, sentence_events, wav_parts
    return asyncio.run(generate())


def validate_voice_audio(path, duration):
    """Reject silent, near-silent or clipped narration before rendering."""
    loudness = qc_module.probe_loudness(path)
    mean_db = loudness.get("mean_db")
    peak_db = loudness.get("peak_db")
    print(
        f"VO audio check: {float(duration):.2f}s, mean {mean_db} dBFS, "
        f"peak {peak_db} dBFS"
    )
    if float(duration) < 1.0:
        raise RuntimeError("The generated voice-over is shorter than one second.")
    if mean_db is None or mean_db <= -45.0:
        raise RuntimeError(
            "The generated voice-over is effectively silent "
            f"(mean {mean_db} dBFS). Rendering stopped."
        )
    if peak_db is None or peak_db <= -30.0:
        raise RuntimeError(
            "The generated voice-over has no usable level "
            f"(peak {peak_db} dBFS). Rendering stopped."
        )
    if peak_db >= 0.0:
        print(
            "VO audio warning: the raw narration peaks at or above 0 dBFS; the "
            "final mix limiter handles it, but the TTS output is hot."
        )
    return loudness


def build_voice_and_subs(script, enforce_minimum=True):
    """Speechify-first calm/deep narration, Edge TTS backup, and duration check.

    When ``enforce_minimum`` is False the measured VO is returned even if it is
    shorter than the format minimum, so the orchestrator can extend the script
    or slow the delivery and regenerate instead of padding with silence.
    """
    sentences = split_sentences(script)
    if not sentences:
        raise RuntimeError("The script contains no speakable sentences.")

    # Clear partial artifacts from a previous failed Speechify attempt.
    for pattern in ("tts_*.wav", "tts_*.mp3", "tts_*_speechify_raw.wav", "gap_*.wav"):
        for stale in OUT.glob(pattern):
            stale.unlink(missing_ok=True)

    allow_fallback = os.getenv("ALLOW_VOICE_FALLBACK", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    voice_id_used = VOICE
    voice_model_used = "edge-tts"

    if SPEECHIFY_KEYS:
        try:
            sentence_word_events, sentence_events, wav_parts = _generate_speechify_parts(sentences)
            voice_provider = "Speechify"
            voice_id_used = SPEECHIFY_VOICE_USED or SPEECHIFY_VOICE_ID or "catalog-default"
            voice_model_used = SPEECHIFY_MODEL
            print(
                f"Voice-over provider: Speechify (model {SPEECHIFY_MODEL}, "
                f"voice {voice_id_used}, locale {LANGUAGE['speechify_locale']})."
            )
        except Exception as exc:
            # MM7: never substitute another TTS engine silently. A Speechify
            # failure is a production failure unless fallback is explicitly
            # authorised for local development.
            for pattern in ("tts_*.wav", "tts_*.mp3", "tts_*_speechify_raw.wav", "gap_*.wav"):
                for stale in OUT.glob(pattern):
                    stale.unlink(missing_ok=True)
            if not allow_fallback:
                raise RuntimeError(
                    "Speechify voice-over failed and no other provider may be "
                    "substituted automatically. Fix the Speechify credential/"
                    "account error and re-run, or set ALLOW_VOICE_FALLBACK=1 for a "
                    "local test only. Original error: " + str(exc)[:600]
                ) from exc
            print(
                "Speechify could not complete the full narration; "
                f"ALLOW_VOICE_FALLBACK is set, restarting the complete VO with Edge TTS: {str(exc)[:500]}"
            )
            sentence_word_events, sentence_events, wav_parts = _generate_edge_parts(sentences)
            voice_provider = "Edge TTS fallback"
            print(f"Voice-over provider: Edge TTS fallback ({VOICE}); Speechify was not used.")
    else:
        print(
            "SPVO_API / SPEECHIFY_API_KEY is not configured, so Speechify cannot "
            "produce the production voice-over. Add the Speechify API key as a "
            "secret; this run uses the local Edge TTS fallback only."
        )
        sentence_word_events, sentence_events, wav_parts = _generate_edge_parts(sentences)
        voice_provider = "Edge TTS fallback (Speechify not configured)"
        print(f"Voice-over provider: Edge TTS fallback ({VOICE}).")

    ending = _create_silence(OUT / "ending_hold.wav", ENDING_HOLD)
    if ending:
        wav_parts.append(ending)

    total_duration = sum(ffprobe_duration(part) for part in wav_parts)
    if total_duration < MIN_VIDEO_DURATION:
        # MM6: never pad the timeline with silence. The orchestrator extends the
        # script or slows delivery naturally, then regenerates the whole VO.
        print(
            f"VO timeline is {total_duration:.2f}s, below the "
            f"{MIN_VIDEO_DURATION:.0f}s format minimum. No silence padding is added; "
            "the script/delivery must be adjusted and the VO regenerated."
        )

    concat = OUT / "voice_concat.txt"
    concat.write_text(
        "".join(f"file '{part.resolve()}'\n" for part in wav_parts),
        encoding="utf-8",
    )
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(OUT / "voiceover.wav"),
    ])
    run([
        "ffmpeg", "-y", "-i", str(OUT / "voiceover.wav"),
        "-c:a", "libmp3lame", "-b:a", "192k", str(OUT / "voiceover.mp3"),
    ])

    # Pre-render audio inspection: a silent or clipped narration must never reach
    # the editing stage just because the TTS request returned bytes.
    validate_voice_audio(OUT / "voiceover.wav", total_duration)

    # Reference-style subtitle block: bold, centred, above the Reels UI zone.
    lines = reference_style.caption_ass_header(CAPTION_STYLE)
    for sentence_group in sentence_word_events:
        for group in build_caption_groups(sentence_group):
            start = group[0]["offset"]
            end = max(group[-1]["offset"] + group[-1]["duration"], start + 0.70)
            phrase = " ".join(str(item["text"]) for item in group)
            phrase = reference_style.format_caption_text(
                phrase, uppercase=CAPTION_STYLE["uppercase"]
            )
            phrase = r"\N".join(textwrap.wrap(
                phrase,
                width=CAPTION_MAX_CHARS,
                break_long_words=False,
                break_on_hyphens=False,
            ))
            phrase = phrase.replace("{", r"\{").replace("}", r"\}")
            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{phrase}"
            )
    (OUT / "captions.ass").write_text("\n".join(lines), encoding="utf-8")
    Path("timing.json").write_text(
        json.dumps({
            "sentences": sentence_events,
            "total_duration": total_duration,
            "minimum_duration": MIN_VIDEO_DURATION,
            "maximum_duration": None,
            "voice_provider": voice_provider,
            "voice": voice_id_used,
            "voice_model": voice_model_used,
            "language": SCRIPT_LANGUAGE,
            "speechify_used": voice_provider == "Speechify",
        }, indent=2),
        encoding="utf-8",
    )

    # MM7: keep a complete record of the narration audio that was generated, so
    # a later audit can prove which provider/voice/model produced which file.
    voice_manifest = {
        "provider": voice_provider,
        "speechify_used": voice_provider == "Speechify",
        "voice": voice_id_used,
        "model": voice_model_used,
        "language": SCRIPT_LANGUAGE,
        "locale": LANGUAGE.get("speechify_locale") or LANGUAGE.get("edge_voice"),
        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "script_words": len(script.split()),
        "total_duration": round(float(total_duration), 3),
        "audio_files": [str(OUT / "voiceover.wav"), str(OUT / "voiceover.mp3")],
        "sentences": [
            {
                "index": index,
                "start": round(float(event["start"]), 3),
                "duration": round(float(event["end"]) - float(event["start"]), 3),
                "text": sentence,
            }
            for index, (event, sentence) in enumerate(
                zip(sentence_events, sentences), 1
            )
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (OUT / "voiceover_manifest.json").write_text(
        json.dumps(voice_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    PIPELINE_STATE["voice_provider"] = voice_provider
    PIPELINE_STATE["voice"] = voice_id_used
    PIPELINE_STATE["voice_model"] = voice_model_used
    PIPELINE_STATE["language"] = SCRIPT_LANGUAGE
    PIPELINE_STATE["voiceover_manifest"] = str(OUT / "voiceover_manifest.json")
    if enforce_minimum and total_duration + 0.001 < MIN_VIDEO_DURATION:
        raise RuntimeError(
            f"Duration invariant failed: final VO timeline is {total_duration:.3f}s; "
            f"minimum is {MIN_VIDEO_DURATION:.0f}s."
        )
    print(
        f"VO timeline ready: {total_duration:.2f}s "
        f"(minimum {MIN_VIDEO_DURATION:.0f}s; no maximum duration)."
    )
    return sentence_events, total_duration


# ============================================================
# SCENE TIMING
# ============================================================

def scene_durations(
    plan,
    sentence_events,
    total_duration
):
    """Each scene owns the pause after its last sentence."""
    result = []

    for index, scene in enumerate(plan["scenes"]):
        start_i = scene["sentence_start"]
        start = sentence_events[start_i]["start"]

        if index + 1 < len(plan["scenes"]):
            next_start_i = (
                plan["scenes"][index + 1]["sentence_start"]
            )

            end = sentence_events[next_start_i]["start"]

        else:
            end = total_duration

        if end <= start:
            raise RuntimeError(
                "Invalid scene timing generated from sentence map."
            )

        result.append((
            start,
            end,
            end - start
        ))

    first_end = result[0][1]

    result[0] = (
        0.0,
        first_end,
        first_end
    )

    return result


# ============================================================
# VIDEO RENDERING
# ============================================================

def render_footage_scene(
    source,
    duration,
    output,
    smart_crop_plan=None,
):
    source_info = ffprobe_video_info(source)
    native_portrait = is_native_portrait_916(
        source_info["width"], source_info["height"],
        aspect_ratio=source_info["aspect_ratio"],
    )

    if native_portrait:
        vf = (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={FPS},setsar=1,format=yuv420p"
        )
    else:
        if not isinstance(smart_crop_plan, dict):
            raise RuntimeError(
                "Render refused non-native-9:16 footage without a verified AI smart-crop plan."
            )
        crop_width, crop_height, x_expression = smart_crop_geometry(source_info, smart_crop_plan)
        vf = (
            f"crop={crop_width}:{crop_height}:x='{x_expression}':y=0:exact=1,"
            f"scale={W}:{H}:flags=lanczos,fps={FPS},setsar=1,format=yuv420p"
        )

    source_duration = ffprobe_duration(source)
    loop_args = ["-stream_loop", "-1"] if source_duration < duration + 0.1 else []

    cmd = [
        "ffmpeg", "-y", *loop_args, "-i", str(source),
        "-t", f"{duration:.3f}", "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", X264_PRESET, "-crf", "20",
        "-r", str(FPS), "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ]
    run(cmd)


def build_sfx_track(total_duration, scene_starts):
    """Mix locally synthesised whooshes at cuts plus one hook impact.

    Returns the path of the SFX stem, or None when sound design is disabled or
    FFmpeg's synthesis filters are unavailable.
    """
    if not SFX_ENABLED or not reference_style.music_bed_available():
        return None

    whoosh = reference_style.build_whoosh(OUT / "sfx_whoosh.wav", run_command=run)
    impact = reference_style.build_impact(OUT / "sfx_impact.wav", run_command=run)

    inputs = [["-i", str(impact)]]
    delays = [550]  # hook lands with the first title fade-in
    for start in list(scene_starts)[1:]:
        try:
            delay_ms = int(round(float(start) * 1000))
        except (TypeError, ValueError):
            continue
        if 0 <= delay_ms < int(total_duration * 1000):
            inputs.append(["-i", str(whoosh)])
            delays.append(delay_ms)

    labels = []
    graph = []
    for index, delay in enumerate(delays):
        graph.append(f"[{index}:a]adelay={delay}|{delay}[s{index}]")
        labels.append(f"[s{index}]")
    graph.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0,"
        # apad keeps the stem exactly as long as the video; otherwise amix
        # would end at the last whoosh and shorten the final mix.
        + f"volume={SFX_GAIN_DB}dB,apad,aresample=48000[out]"
    )

    output = OUT / "sfx_track.wav"
    command = ["ffmpeg", "-y"]
    for pair in inputs:
        command.extend(pair)
    command += [
        "-filter_complex", ";".join(graph),
        "-map", "[out]",
        "-t", f"{float(total_duration):.3f}",
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
        str(output),
    ]
    run(command)
    return output


def render(plan, sentence_events, total_duration):
    if float(total_duration) + 0.001 < MIN_VIDEO_DURATION:
        raise RuntimeError(
            f"Refusing to render a video shorter than {MIN_VIDEO_DURATION:.0f}s."
        )
    # There is deliberately no upper-duration guard: use the complete VO timeline.
    spans = scene_durations(
        plan,
        sentence_events,
        total_duration
    )

    segment_files = []

    for i, (scene, (_, _, duration)) in enumerate(
        zip(plan["scenes"], spans),
        1
    ):
        duration = max(0.8, duration)

        output = OUT / f"segment_{i:02d}.mp4"
        source = scene.get("media_file")

        if source and Path(source).exists():
            render_footage_scene(
                source,
                duration,
                output,
                smart_crop_plan=scene.get("smart_vertical_reframe")
                if isinstance(scene.get("smart_vertical_reframe"), dict)
                and scene.get("smart_vertical_reframe", {}).get("mode") == "ai_content_aware_dynamic_crop"
                else None,
            )

        else:
            raise RuntimeError(
                f"Scene {i} has no approved real-world footage. "
                "Rendering stopped rather than substituting a diagram "
                "or unrelated visual."
            )

        segment_files.append(output)

        print(
            f"Scene {i}/8 rendered: {duration:.2f}s"
        )

    concat = OUT / "video_concat.txt"

    concat.write_text(
        "".join(
            f"file '{p.resolve()}'\n"
            for p in segment_files
        ),
        encoding="utf-8"
    )

    base = OUT / "video_base.mp4"

    run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat),
        "-c", "copy",
        str(base)
    ])

    final = Path("documentary.mp4")

    music_file = None
    if MUSIC_BED_ENABLED:
        try:
            music_file = reference_style.build_music_bed(
                total_duration, OUT / "music_bed.wav", run_command=run
            )
            print(
                f"Music bed ready ({total_duration:.1f}s, original FFmpeg synthesis, "
                f"{MUSIC_GAIN_DB:.0f} dB under the narration)."
            )
        except Exception as exc:
            print(f"Music bed synthesis failed; continuing with narration only: {exc}")
            music_file = None

    sfx_file = None
    if SFX_ENABLED:
        try:
            sfx_file = build_sfx_track(
                total_duration, [span[0] for span in spans]
            )
        except Exception as exc:
            print(f"Sound design synthesis failed; continuing without it: {exc}")
            sfx_file = None

    # The opening hook is an ASS event, not drawtext: libass is required for the
    # subtitles anyway and the bundled fonts guarantee identical typography.
    hook_text = re.sub(r"\s+", " ", str(plan.get("hook") or "")).strip()
    if hook_text:
        reference_style.append_hook_caption(
            OUT / "captions.ass", hook_text, CAPTION_STYLE
        )

    fonts_dir = Path(__file__).with_name("fonts")
    captions_path = str((OUT / "captions.ass").resolve()).replace(":", r"\:")
    fonts_arg = ""
    if fonts_dir.is_dir():
        fonts_arg = ":fontsdir=" + str(fonts_dir).replace(":", r"\:")

    inputs = ["-i", str(base), "-i", str(OUT / "voiceover.wav")]
    graph_parts = [
        "[0:v]subtitles=" + captions_path + fonts_arg + "[v]",
        "[1:a]highpass=f=65,lowpass=f=15000,"
        "acompressor=threshold=-22dB:ratio=2.5:attack=15:release=220:makeup=1.1"
        + ("[vo0]" if music_file else "[vo]"),
    ]
    music_index = None
    sfx_index = None
    if music_file:
        music_index = len(inputs) // 2
        inputs += ["-i", str(music_file)]
        # The narration feeds both the mix and the ducker, so it must be split.
        graph_parts.append("[vo0]asplit=2[vo][vosc]")
        graph_parts.append(
            f"[{music_index}:a]volume={MUSIC_GAIN_DB}dB[mus]"
        )
        graph_parts.append(
            "[mus][vosc]sidechaincompress=threshold=0.032:ratio=8:"
            "attack=12:release=320:makeup=1[musduck]"
        )
        graph_parts.append(
            "[vo][musduck]amix=inputs=2:duration=first:normalize=0[mixed]"
        )
        mix_label = "[mixed]"
    else:
        mix_label = "[vo]"

    if sfx_file:
        sfx_index = len(inputs) // 2
        inputs += ["-i", str(sfx_file)]
        graph_parts.append(
            f"{mix_label}[{sfx_index}:a]amix=inputs=2:duration=first:normalize=0[mixedsfx]"
        )
        mix_label = "[mixedsfx]"

    # Loudness normalisation runs before the limiter so the delivered peak is
    # controlled after gain changes (previous order could re-introduce peaks).
    graph_parts.append(
        f"{mix_label}loudnorm=I=-15:TP=-1.5:LRA=9,"
        "alimiter=limit=0.95,aresample=48000[a]"
    )

    run([
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(graph_parts),
        "-map", "[v]",
        "-map", "[a]",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264",
        "-preset", X264_PRESET,
        "-crf", X264_CRF,
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(final)
    ])
    PIPELINE_STATE["music_bed"] = bool(music_file)
    PIPELINE_STATE["sound_design"] = bool(sfx_file)

    final_duration = ffprobe_duration(final)
    if final_duration + 0.15 < MIN_VIDEO_DURATION:
        raise RuntimeError(
            f"Rendered file is only {final_duration:.2f}s; "
            f"the required minimum is {MIN_VIDEO_DURATION:.0f}s."
        )
    if final_duration + 0.20 < total_duration:
        raise RuntimeError(
            f"Rendered video ({final_duration:.2f}s) ended before the complete "
            f"voice-over timeline ({total_duration:.2f}s)."
        )
    print(
        f"Final video: {final}; duration {final_duration:.2f}s "
        f"(complete VO timeline {total_duration:.2f}s; no upper limit)."
    )


# ============================================================
# MAIN
# ============================================================

def _write_json_report(path, payload):
    try:
        Path(path).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as report_error:
        print(f"Could not write diagnostic report {path}: {report_error}")


def start_pipeline_stage(name):
    PIPELINE_STATE["status"] = "running"
    PIPELINE_STATE["current_stage"] = name
    PIPELINE_STATE["stages"].append({
        "name": name,
        "status": "running",
        "started_at_epoch": time.time(),
    })
    _write_json_report("stage_report.json", PIPELINE_STATE)
    print(f"Pipeline stage started: {name}")


def finish_pipeline_stage():
    if PIPELINE_STATE["stages"]:
        stage = PIPELINE_STATE["stages"][-1]
        stage["status"] = "completed"
        stage["finished_at_epoch"] = time.time()
        stage["duration_seconds"] = round(
            stage["finished_at_epoch"] - stage["started_at_epoch"], 2
        )
    _write_json_report("stage_report.json", PIPELINE_STATE)


def write_failure_diagnostics(exc):
    PIPELINE_STATE["status"] = "failed"
    if PIPELINE_STATE.get("stages"):
        active_stage = PIPELINE_STATE["stages"][-1]
        if active_stage.get("status") == "running":
            active_stage["status"] = "failed"
            active_stage["finished_at_epoch"] = time.time()
            active_stage["failure_type"] = type(exc).__name__
            active_stage["failure_message"] = str(exc)
    failure = {
        "status": "failed",
        "current_stage": PIPELINE_STATE.get("current_stage", "unknown"),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": traceback.format_exc(),
        "finished_at_epoch": time.time(),
        "configuration": {
            "project_release": "MM6",
            "visual_verification_required": VISION_REQUIRED,
            "groq_keys_configured": len(GROQ_KEYS),
            "openrouter_keys_configured": len(OPENROUTER_KEYS),
            "gemini_keys_configured": len(GEMINI_KEYS),
            "cloudflare_key_pairs_configured": len(CLOUDFLARE_CREDENTIALS),
            "speechify_keys_configured": len(SPEECHIFY_KEYS),
            "pixabay_key_configured": bool(PIXABAY_KEY),
            "pexels_key_configured": bool(PEXELS_KEY),
            "release": "MM6",
            "source_priority": list(FOOTAGE_SOURCE_PRIORITY),
            "footage_source_requests": dict(FOOTAGE_SOURCE_REQUEST_COUNTS),
            "footage_source_cooldowns_seconds": {
                source: int(footage_source_cooldown_remaining(source))
                for source in FOOTAGE_SOURCE_PRIORITY
                if footage_source_cooldown_remaining(source) > 0
            },
        },
    }
    PIPELINE_STATE["failure"] = {
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "finished_at_epoch": failure["finished_at_epoch"],
    }
    _write_json_report("failure_report.json", failure)
    _write_json_report("stage_report.json", PIPELINE_STATE)


class _TeeStream:
    """Write console output to both the Actions log and output/pipeline.log."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return False


def cleanup_generated_markdown():
    """Remove generated upgrade/validation Markdown anywhere in the repo.

    Deliberately preserves README.md, SCRIPTWRITER_MASTER_PROMPT.md, and all
    unrelated documentation. Runs from the actual project root, not cwd.
    """
    root = Path(__file__).resolve().parent
    removed = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {
            ".git", ".venv", "venv", "node_modules", "__pycache__"
        }]
        for filename in filenames:
            lower = filename.lower()
            if not lower.endswith(".md"):
                continue
            is_upgrade_note = "upgrade" in lower
            is_validation_report = "validation" in lower and "report" in lower
            if not (is_upgrade_note or is_validation_report):
                continue
            path = Path(directory) / filename
            try:
                path.unlink()
                removed.append(str(path.relative_to(root)))
            except OSError as exc:
                print(f"Markdown cleanup could not remove {path.name}: {exc}")
    if removed:
        print("Markdown cleanup removed: " + ", ".join(removed))
    else:
        print("Markdown cleanup: no generated upgrade/validation files found.")
    return removed


def validate_runtime_config():
    if not (GROQ_KEYS or OPENROUTER_KEYS or GEMINI_KEYS or CLOUDFLARE_CREDENTIALS):
        raise RuntimeError(
            "Missing AI provider credentials. Add one or more Groq, OpenRouter, "
            "Gemini, or Cloudflare AI token/account pairs as GitHub Actions secrets."
        )
    if not TOPIC:
        raise RuntimeError("Enter a topic in the Run workflow form.")


def main():
    global VO_RATE_OVERRIDE
    cleanup_generated_markdown()
    validate_runtime_config()

    start_pipeline_stage("research")
    print(f"1/8 Researching credible sources for: {TOPIC}")
    research_data = research_module.research_topic(TOPIC)
    research_module.write_research_files(research_data)
    for warning in research_data.get("warnings") or []:
        print(f"Research warning: {warning}")
    print(
        f"Research complete: {research_data['verified_source_count']} "
        f"source article(s) stored in research.json."
    )
    finish_pipeline_stage()

    start_pipeline_stage("documentary_plan_generation")
    print("2/8 Generating documentary plan from the research material...")
    plan = gemini_plan(TOPIC, research_brief=research_data.get("brief"))
    finish_pipeline_stage()

    start_pipeline_stage("fact_checking")
    print("3/8 Fact-checking claims against the research material...")
    verification = fact_check_plan(plan, research_data)
    research_module.write_research_files(research_data, verification)
    plan["fact_check"] = {
        "overall": verification["overall"],
        "checked": verification.get("checked", 0),
        "supported": verification.get("supported", 0),
        "unsupported": verification.get("unsupported", 0),
        "contradicted": verification.get("contradicted", 0),
        "claim_source": verification.get("claim_source"),
    }
    Path("plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"Fact check stored in fact_check.json "
        f"(overall {verification['overall']}, "
        f"{verification.get('checked', 0)} claim(s) checked)."
    )
    finish_pipeline_stage()

    # MM7 pipeline order: the voice-over is produced from the verified script
    # *before* footage discovery, so visual search follows the narration that
    # will actually be spoken, and rendering only starts once both exist.
    start_pipeline_stage("voiceover_and_subtitle_generation")
    print("4/8 Generating the verified voice-over and subtitles...")
    sentence_events, total_duration = build_voice_and_subs(
        plan["script"], enforce_minimum=False
    )

    # MM6: the 120-word floor does not guarantee 70 seconds of speech. Measure
    # the real VO, deepen the script or slow delivery naturally, regenerate.
    attempts = 0
    while total_duration + 0.05 < MIN_VIDEO_DURATION and attempts < 2:
        attempts += 1
        print(
            f"Voice-over is {total_duration:.2f}s; extending the script "
            f"(attempt {attempts}/2) instead of padding with silence."
        )
        try:
            plan = extend_plan_for_duration(plan, research_data.get("brief"))
        except Exception as exc:
            print(f"Script extension unavailable: {str(exc)[:300]}")
            break
        Path("plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # The deepened wording can introduce new claims: verify it again before
        # it is spoken and rendered.
        try:
            verification = fact_check_plan(plan, research_data)
            research_module.write_research_files(research_data, verification)
            plan["fact_check"] = {
                "overall": verification["overall"],
                "checked": verification.get("checked", 0),
                "supported": verification.get("supported", 0),
                "unsupported": verification.get("unsupported", 0),
                "contradicted": verification.get("contradicted", 0),
                "claim_source": verification.get("claim_source"),
            }
        except Exception as exc:
            print(f"Re-verification of the extended script failed: {str(exc)[:400]}")
            raise
        sentence_events, total_duration = build_voice_and_subs(
            plan["script"], enforce_minimum=False
        )

    if total_duration + 0.05 < MIN_VIDEO_DURATION:
        print(
            "Duration recovery: trying a slower but still natural delivery pace "
            f"({os.getenv('TTS_RATE_RECOVERY', '-12%')})."
        )
        VO_RATE_OVERRIDE = os.getenv("TTS_RATE_RECOVERY", "-12%").strip() or "-12%"
        sentence_events, total_duration = build_voice_and_subs(
            plan["script"], enforce_minimum=False
        )

    if total_duration + 0.05 < MIN_VIDEO_DURATION:
        raise RuntimeError(
            f"Voice-over is {total_duration:.2f}s after script extension and a "
            f"natural slower delivery; the format requires at least "
            f"{MIN_VIDEO_DURATION:.0f}s. The run is stopped rather than padding "
            "the timeline with silence or empty footage."
        )
    print(
        f"Voice-over verified: {total_duration:.2f}s of narration "
        f"(minimum {MIN_VIDEO_DURATION:.0f}s, ends with the narration)."
    )
    finish_pipeline_stage()

    start_pipeline_stage("footage_search_download_and_visual_verification")
    print("5/8 Searching, downloading and visually verifying footage...")
    fetch_footage(plan)
    finish_pipeline_stage()

    start_pipeline_stage("video_rendering")
    print("6/8 Editing scenes, mixing audio and rendering...")
    render(plan, sentence_events, total_duration)
    finish_pipeline_stage()

    start_pipeline_stage("quality_control")
    print("7/8 Running final quality control...")
    qc_report = qc_module.run_qc(
        "documentary.mp4", plan, total_duration,
        captions_path=str(OUT / "captions.ass"),
    )
    qc_module.write_qc_report(qc_report)
    copyright_report = reference_style.build_copyright_report(
        plan,
        SCRIPT_LANGUAGE,
        PIPELINE_STATE.get("voice_provider", "unknown"),
        reference_style.MUSIC_BED_PROVENANCE
        if PIPELINE_STATE.get("music_bed")
        else None,
    )
    reference_style.write_copyright_report(
        "copyright_report.json", copyright_report
    )
    finish_pipeline_stage()

    PIPELINE_STATE["status"] = "completed"
    PIPELINE_STATE["current_stage"] = "complete"
    _write_json_report("stage_report.json", PIPELINE_STATE)
    print(
        "8/8 Production complete: documentary.mp4, plan.json, credits.txt, "
        "timing.json, research.json, fact_check.json, copyright_report.json, "
        "qc_report.json"
    )


def run_with_diagnostics():
    log_path = OUT / "pipeline.log"
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    with log_path.open("w", encoding="utf-8", buffering=1) as log_file:
        sys.stdout = _TeeStream(original_stdout, log_file)
        sys.stderr = _TeeStream(original_stderr, log_file)
        try:
            main()
        except BaseException as exc:
            write_failure_diagnostics(exc)
            traceback.print_exc()
            raise
        finally:
            try:
                cleanup_generated_markdown()
            except Exception as cleanup_exc:
                print(f"Markdown cleanup warning: {cleanup_exc}", file=original_stderr)
            sys.stdout = original_stdout
            sys.stderr = original_stderr


if __name__ == "__main__":
    run_with_diagnostics()

