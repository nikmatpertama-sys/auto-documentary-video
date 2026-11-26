
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
from pathlib import Path
from urllib.parse import quote, urlparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import edge_tts

# ============================================================
# CONFIG
# ============================================================
OUT = Path("output")
FOOT = OUT / "footage"
CACHE = Path(".api-cache")

for folder in (OUT, FOOT, CACHE):
    folder.mkdir(parents=True, exist_ok=True)

GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
PEXELS_KEY = os.getenv("PEXELS_API_KEY", "").strip()
TOPIC = os.getenv("INPUT_TOPIC", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "openai/gpt-oss-120b"
).strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "openrouter/free"
).strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural").strip()
BASE_RATE = os.getenv("TTS_RATE", "-4%").strip()
BASE_PITCH = os.getenv("TTS_PITCH", "-3Hz").strip()

W, H, FPS = 1080, 1920, 30
TARGET_SOURCE_ASPECT = 9 / 16
SOURCE_ASPECT_TOLERANCE = 0.003  # native portrait 9:16 only; no landscape/4:5 crop
MIN_CLIP_SHORT_SIDE = 640
CAPTION_MAX_WORDS = 5
CAPTION_MAX_CHARS = 32
X264_PRESET = os.getenv("X264_PRESET", "ultrafast").strip() or "ultrafast"

OPENING_LEAD = 0.75
SENTENCE_GAP = 0.30
HOOK_GAP = 0.70
ENDING_HOLD = 1.25

MIN_CLIP_WIDTH = 640
MAX_CLIP_BYTES = 180 * 1024 * 1024

# Visual verification is deliberately fail-closed by default. Set this to
# false only if you explicitly accept metadata-only footage selection.
VISION_REQUIRED = os.getenv("FOOTAGE_VISION_REQUIRED", "true").strip().lower() not in (
    "0", "false", "no", "off"
)

# Search more of the metadata-ranked pool before declaring a scene impossible.
VISION_CANDIDATE_LIMIT = max(
    1, min(30, int(os.getenv("FOOTAGE_VISION_CANDIDATE_LIMIT", "26")))
)

# Version 10.13: preserve hook/subtitle readability and reject non-native 9:16 footage.
# Version 10.12: recover from Wikimedia download throttling and search sparse tiers better.
# Internet Archive receives the largest candidate budget; the other sources
# are fallback tiers, ordered from open archives to stock-footage providers.
FOOTAGE_SOURCE_PRIORITY = (
    "Internet Archive",
    "Wikimedia Commons",
    "NASA",
    "Pexels",
    "Pixabay",
)
FOOTAGE_SOURCE_CANDIDATE_LIMITS = {
    "Internet Archive": 10,
    "Wikimedia Commons": 4,
    "NASA": 8,
    "Pexels": 8,
    "Pixabay": 8,
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
WIKIMEDIA_LAST_REQUEST_AT = 0.0
WIKIMEDIA_MIN_REQUEST_INTERVAL = 1.2
WIKIMEDIA_MAX_RETRIES = 2

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
    """Read Retry-After (seconds or HTTP-date), with a bounded safe fallback."""
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
    return max(15.0, min(900.0, float(delay)))


def mark_footage_source_rate_limited(source, response=None):
    """Put a throttled footage source in a timed cooldown for search and download."""
    if not source:
        return 0.0
    default = 180.0 if source == "Wikimedia Commons" else 60.0
    delay = response_retry_after_seconds(response, default)
    until = time.monotonic() + delay
    FOOTAGE_SOURCE_COOLDOWN_UNTIL[source] = max(
        FOOTAGE_SOURCE_COOLDOWN_UNTIL.get(source, 0.0), until
    )
    print(
        f"{source} rate-limited; cooling this source for "
        f"{delay:.0f}s before trying it again."
    )
    return delay


def is_wikimedia_url(url):
    """Recognize both Commons API and upload.wikimedia.org media URLs."""
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == "wikimedia.org" or host.endswith(".wikimedia.org")


def pace_wikimedia_request():
    """Apply one shared request interval to Commons API and media downloads."""
    global WIKIMEDIA_LAST_REQUEST_AT
    elapsed = time.monotonic() - WIKIMEDIA_LAST_REQUEST_AT
    wait_for_interval = WIKIMEDIA_MIN_REQUEST_INTERVAL - elapsed
    if wait_for_interval > 0:
        time.sleep(wait_for_interval)
    WIKIMEDIA_LAST_REQUEST_AT = time.monotonic()


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


# ============================================================
# AI DOCUMENTARY PLANNER
# ============================================================

def documentary_prompt(topic):
    """Load the standalone single-role professional scriptwriter master prompt."""
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

    return prompt.replace("[[TOPIC]]", str(topic or "").strip())


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

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")

        raise RuntimeError(
            f"{provider} HTTP {response.status_code}: {detail}"
        )

    data = response.json()

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


def gemini_plan(topic):
    """Generate the plan with automatic provider failover."""
    prompt = documentary_prompt(topic)

    providers = []

    if GROQ_KEY:
        providers.append((
            "Groq",
            lambda: call_openai_compatible(
                "Groq",
                GROQ_KEY,
                GROQ_MODEL,
                prompt
            ),
        ))

    if OPENROUTER_KEY:
        providers.append((
            "OpenRouter",
            lambda: call_openai_compatible(
                "OpenRouter",
                OPENROUTER_KEY,
                OPENROUTER_MODEL,
                prompt
            ),
        ))

    if GEMINI_KEY:
        providers.append((
            "Gemini",
            lambda: call_gemini(
                GEMINI_KEY,
                MODEL,
                prompt
            ),
        ))

    if not providers:
        raise RuntimeError(
            "No AI provider API key found. Add at least one GitHub "
            "Secret: GROQ_API_KEY, OPENROUTER_API_KEY, or GEMINI_API_KEY."
        )

    errors = []
    plan = None

    for index, (provider_name, generate) in enumerate(providers, 1):
        print(
            f"Trying AI provider {index}/{len(providers)}: "
            f"{provider_name}..."
        )

        try:
            candidate = generate()

            candidate["scenes"] = candidate.get("scenes", [])

            if (
                not isinstance(candidate["scenes"], list)
                or len(candidate["scenes"]) != 8
            ):
                raise RuntimeError(
                    "AI returned an incomplete plan. "
                    "Expected exactly 8 scenes."
                )

            scene_narrations = []

            for scene_index, scene in enumerate(
                candidate["scenes"],
                1
            ):
                if not isinstance(scene, dict):
                    raise RuntimeError(
                        f"Scene {scene_index} is not a JSON object."
                    )

                narration = re.sub(
                    r"\s+",
                    " ",
                    str(scene.get("narration", ""))
                ).strip()

                if not narration:
                    raise RuntimeError(
                        f"Scene {scene_index} has empty narration."
                    )

                scene["narration"] = narration
                scene_narrations.append(narration)

            candidate["script"] = " ".join(
                scene_narrations
            ).strip()

            word_count = len(
                candidate["script"].split()
            )

            if not 100 <= word_count <= 190:
                raise RuntimeError(
                    f"Scene narration totals {word_count} words; "
                    "expected 100-190."
                )

            candidate["scenes"][0]["visual_type"] = "footage"

            assign_scene_sentence_ranges(candidate)

            plan = candidate

            print(
                f"Documentary plan generated successfully by {provider_name}."
            )
            break

        except Exception as exc:
            message = f"{provider_name}: {exc}"
            errors.append(message)

            print(message)

            if index < len(providers):
                print(
                    "Switching automatically to the next AI provider..."
                )

    if plan is None:
        raise RuntimeError(
            "All configured AI providers failed:\n- "
            + "\n- ".join(errors)
        )

    Path("plan.json").write_text(
        json.dumps(
            plan,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(
        f"Script ready: {len(plan['script'].split())} words, "
        f"{len(plan['scenes'])} scenes."
    )

    return plan


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

def download_stream(url, dest, source=None):
    """Download safely and cool down a source immediately after HTTP 429."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    is_commons = is_wikimedia_url(url)
    source = "Wikimedia Commons" if is_commons else source

    try:
        if is_commons:
            # Wikimedia throttles media downloads separately from API searches.
            # Pace both through the same clock; do not hammer every candidate
            # after the first upload.wikimedia.org 429 response.
            pace_wikimedia_request()

        with requests.get(
            url,
            stream=True,
            timeout=(25, 180),
            headers={
                "User-Agent": "AutoDocumentaryVideo/3.1 (documentary footage client)"
            }
        ) as response:

            if response.status_code == 429:
                mark_footage_source_rate_limited(source, response)

            response.raise_for_status()

            declared = int(
                response.headers.get("Content-Length") or 0
            )

            if declared > MAX_CLIP_BYTES:
                raise RuntimeError(
                    f"Clip is {declared / 1024 / 1024:.1f} MB, "
                    f"above the {MAX_CLIP_BYTES / 1024 / 1024:.0f} MB limit."
                )

            total = 0

            with open(dest, "wb") as f:
                for chunk in response.iter_content(
                    1024 * 1024
                ):
                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > MAX_CLIP_BYTES:
                        raise RuntimeError(
                            f"Clip exceeded "
                            f"{MAX_CLIP_BYTES / 1024 / 1024:.0f} MB safety limit."
                        )

                    f.write(chunk)

        if dest.stat().st_size < 50000:
            raise RuntimeError(
                "Downloaded clip is unexpectedly small."
            )

    except Exception:
        dest.unlink(missing_ok=True)
        raise


def clean_query(query):
    return re.sub(
        r"[^a-zA-Z0-9 ]",
        " ",
        str(query)
    ).strip()[:95]


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

    response = requests.get(
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

    response = requests.get(
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
    response = requests.get(
        "https://images-api.nasa.gov/search",
        params={
            "q": query,
            "media_type": "video",
            "page_size": 8
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
            manifest = requests.get(
                f"https://images-api.nasa.gov/asset/{nasa_id}",
                timeout=35
            )

            manifest.raise_for_status()

            assets = manifest.json().get(
                "collection", {}
            ).get("items", [])

        except Exception as exc:
            print(
                f"NASA asset lookup skipped ({nasa_id}): {exc}"
            )
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
    """Search Commons videos with a polite request interval and 429 recovery."""
    global WIKIMEDIA_LAST_REQUEST_AT

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:video {clean_query(query)}",
        "gsrnamespace": 6,
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
    }
    headers = {
        "User-Agent": "AutoDocumentaryVideo/3.1 (video research; Wikimedia Commons API client)"
    }

    response = None
    for attempt in range(WIKIMEDIA_MAX_RETRIES + 1):
        pace_wikimedia_request()

        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            headers=headers,
            timeout=35,
        )
        if response.status_code != 429:
            response.raise_for_status()
            break

        if attempt >= WIKIMEDIA_MAX_RETRIES:
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = 3.0 * (attempt + 1)
        delay = max(2.0, min(15.0, delay))
        print(
            f"Wikimedia Commons returned HTTP 429; "
            f"waiting {delay:.1f}s before retry {attempt + 1}/"
            f"{WIKIMEDIA_MAX_RETRIES}."
        )
        time.sleep(delay)

    pages = response.json().get("query", {}).get("pages", {})
    results = []

    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
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
        metadata = f"{page.get('title', '')} {description}"
        results.append({
            "source": "Wikimedia Commons",
            "id": str(page.get("pageid")),
            "url": url,
            "width": int(info.get("width") or 0),
            "height": int(info.get("height") or 0),
            "duration": 0,
            "metadata": metadata,
            "tags": metadata,
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
        response = requests.get(
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
                ("rows", "15"),
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
    except requests.RequestException as exc:
        if getattr(getattr(exc, "response", None), "status_code", None) == 429:
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
    for item in documents[:12]:
        if len(results) >= 6 or metadata_checks >= 8:
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
                metadata_response = requests.get(
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
            except requests.RequestException as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) == 429:
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
            if file_size <= 50000 or file_size > MAX_CLIP_BYTES:
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
            "url": file_url,
            "width": width,
            "height": int(selected_file.get("height") or 0),
            "duration": parse_runtime(item.get("runtime") or metadata.get("runtime")),
            "metadata": combined_metadata,
            "tags": subject_text or combined_metadata,
            "creator": str(creator),
            "page_url": page_url,
            "license": license_name,
        })

    return results

def search_all_footage_sources(query):
    """Search configured sources; avoid caching partial results after throttling."""
    cache_sources = (
        "footage-sources-v2|internet-archive-v1|"
        + ("pixabay" if PIXABAY_KEY else "")
        + ("|pexels" if PEXELS_KEY else "")
        + "|nasa|commons"
    )
    cache_file = CACHE / (
        hashlib.sha256(
            (cache_sources + "|" + query.lower()).encode()
        ).hexdigest() + ".sources.json"
    )

    if (
        cache_file.exists()
        and time.time() - cache_file.stat().st_mtime < 21600
    ):
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    searches = [
        ("Internet Archive", internet_archive_search),
        ("Wikimedia Commons", commons_search),
        ("NASA", nasa_search),
        ("Pexels", pexels_search),
        ("Pixabay", pixabay_search),
    ]
    combined = []
    complete_search = True

    for source, search_fn in searches:
        cooldown_until = FOOTAGE_SOURCE_COOLDOWN_UNTIL.get(source, 0.0)
        if time.monotonic() < cooldown_until:
            complete_search = False
            remaining = max(1, int(cooldown_until - time.monotonic()))
            print(
                f"  {source} is cooling down after rate limiting; "
                f"retry eligible in about {remaining}s."
            )
            continue
        FOOTAGE_SOURCE_COOLDOWN_UNTIL.pop(source, None)

        # Backward compatibility: an old disabled marker is cleared once its
        # cooldown has elapsed, rather than making the source unavailable forever.
        FOOTAGE_SOURCE_DISABLED.discard(source)

        try:
            found = search_fn(query)
            combined.extend(found)
            if found:
                print(f"  {source}: {len(found)} candidate(s)")

        except Exception as exc:
            status_code = getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status_code == 429:
                response = getattr(exc, "response", None)
                mark_footage_source_rate_limited(source, response)
                complete_search = False
            else:
                complete_search = False
                print(
                    f"  {source} search unavailable for '{query}': {exc}"
                )

    # Partial results must not be cached: otherwise a transient 429 can hide
    # Commons/other sources for the full six-hour cache lifetime.
    if complete_search:
        try:
            cache_file.write_text(
                json.dumps(combined), encoding="utf-8"
            )
        except Exception:
            pass

    return combined


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

    return expanded[:12]


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
        candidates.extend([
            "insect macro nature",
            "insect on leaf closeup",
            "insect crawling plant",
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


def relevance_score(hit, query, narration):
    """Score subject, setting and requested shot type from source metadata."""
    metadata = " ".join([
        str(hit.get("metadata", "")),
        str(hit.get("tags", "")),
        str(hit.get("page_url", "")),
    ])

    metadata_tokens = set(
        normalize_words(metadata)
    )

    query_tokens, query_groups = query_concepts(query)
    narration_tokens, narration_groups = query_concepts(narration)

    if not metadata_tokens or not query_tokens:
        return 0.0, 0, max(1, len(query_groups))

    matched_groups = 0

    for aliases in query_groups.values():
        if metadata_tokens & aliases:
            matched_groups += 1

    direct_hits = query_tokens & metadata_tokens

    group_coverage = (
        matched_groups / max(1, len(query_groups))
    )

    direct_coverage = (
        len(direct_hits) / max(1, len(query_tokens))
    )

    narration_matches = sum(
        1 for aliases in narration_groups.values()
        if metadata_tokens & aliases
    )

    narration_coverage = (
        narration_matches / max(1, min(6, len(narration_groups)))
    )

    camera_aliases = CONCEPT_GROUPS.get("aerial", set())
    camera_requested = bool(
        query_tokens & camera_aliases
    )
    camera_match = bool(
        metadata_tokens & camera_aliases
    )

    camera_bonus = (
        0.10 if camera_requested and camera_match else 0.0
    )
    camera_penalty = (
        0.04 if camera_requested and not camera_match else 0.0
    )

    context_tokens = query_tokens | narration_tokens
    conflicts = 0

    for conflict_terms in CONTEXT_CONFLICTS.values():
        if (
            metadata_tokens & conflict_terms
            and not context_tokens & conflict_terms
        ):
            conflicts += 1

    score = (
        0.58 * group_coverage
        + 0.18 * direct_coverage
        + 0.18 * narration_coverage
        + camera_bonus
        - camera_penalty
    )

    if conflicts:
        score -= min(0.55, 0.30 * conflicts)

    return (
        max(0.0, score),
        matched_groups,
        max(1, len(query_groups))
    )


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

    metadata = " ".join([
        str(hit.get("metadata", "")),
        str(hit.get("tags", "")),
        str(hit.get("page_url", "")),
    ])
    tokens = set(normalize_words(metadata))

    strict_aliases = {
        "rainforest": {"amazon", "rainforest", "jungle", "tropical", "tropics", "equatorial"},
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
    if requested_subjects and not requested_subjects.issubset(matched):
        return False
    if "rainforest" in anchors and "rainforest" not in matched:
        return False
    if "aerial" in anchors and "aerial" not in matched:
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
        for name, aliases in primary.items():
            if name in primary_core and not anchor_matches(name, aliases):
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


def extract_vision_frames(video_path, frame_count=3):
    """Extract evenly spaced, low-resolution frames for visual inspection."""
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

            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-ss", f"{timestamp:.3f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", "scale=768:-2",
                    "-q:v", "5",
                    str(frame_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

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

    if len(frame_paths) < 2:
        raise RuntimeError(
            "Not enough readable frames for visual verification."
        )

    return frame_paths


def _parse_visual_verdict(provider, raw_text):
    """Parse and validate a strict visual-verification JSON response."""
    try:
        verdict = extract_json_object(raw_text)

    except Exception as exc:
        preview = re.sub(
            r"\s+",
            " ",
            str(raw_text or "")
        ).strip()[:350]

        raise VisionResponseError(
            f"{provider} did not return valid JSON: {exc}; "
            f"response preview={preview!r}"
        ) from exc

    if not isinstance(verdict, dict):
        raise VisionResponseError(
            f"{provider} returned JSON, but it was not an object."
        )

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


def _verify_with_gemini(frames, prompt):
    parts = [{"text": prompt}]

    for frame in frames[:2]:
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
            "maxOutputTokens": 512,
        },
    }

    response = post_with_transient_retry(
        url,
        retries=0,
        params={"key": GEMINI_KEY},
        json=payload,
        timeout=(15, 55),
    )

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

    return _parse_visual_verdict(
        "Gemini",
        raw_text
    )


def _verify_with_openai_compatible_vision(
    provider,
    api_key,
    model,
    frames,
    prompt
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
        payload["max_completion_tokens"] = 384

    else:
        payload["max_tokens"] = 768
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

    return _parse_visual_verdict(
        provider,
        raw_text
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


def verify_footage_visually(
    video_path,
    scene,
    query,
    candidate_metadata=""
):
    """Verify visible shot content, using catalog metadata for provenance."""
    frames = extract_vision_frames(
        video_path,
        frame_count=3
    )

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

    if GROQ_KEY:
        # Groq Qwen vision uses 2048 input tokens per image. One representative
        # middle frame reduces TPM use while preserving the principal scene.
        groq_frames = [
            frames[len(frames) // 2]
        ]

        providers.append((
            "Groq",
            "Groq",
            lambda: _verify_with_openai_compatible_vision(
                "Groq",
                GROQ_KEY,
                GROQ_VISION_MODEL,
                groq_frames,
                prompt
            ),
        ))

    if OPENROUTER_KEY:
        models = []

        for model_name in (
            OPENROUTER_VISION_MODEL,
            OPENROUTER_VISION_FALLBACK_MODEL,
        ):
            if (
                model_name
                and model_name not in models
            ):
                models.append(model_name)

        openrouter_frames = (
            [frames[0], frames[-1]]
            if len(frames) > 1
            else frames
        )

        for model_name in models:
            route_id = f"OpenRouter::{model_name}"

            providers.append((
                route_id,
                f"OpenRouter ({model_name})",
                lambda model_name=model_name: (
                    _verify_with_openai_compatible_vision(
                        "OpenRouter",
                        OPENROUTER_KEY,
                        model_name,
                        openrouter_frames,
                        prompt
                    )
                ),
            ))

    if GEMINI_KEY:
        gemini_frames = (
            [frames[0], frames[-1]]
            if len(frames) > 1
            else frames
        )

        providers.append((
            "Gemini",
            f"Gemini ({VISION_MODEL})",
            lambda: _verify_with_gemini(
                gemini_frames,
                prompt
            ),
        ))

    errors = []
    attempted = 0

    for provider_id, provider_name, verify in providers:
        if provider_id in VISION_DISABLED_PROVIDERS:
            print(
                f"Visual verifier: skipping {provider_name}; "
                "route disabled after a hard error."
            )
            continue

        cooldown = _provider_cooldown_remaining(
            provider_id
        )

        if cooldown > 0:
            print(
                f"Visual verifier: {provider_name} cooling down for "
                f"{cooldown:.0f}s; trying the next backup."
            )
            continue

        attempted += 1

        print(
            f"Visual verifier: trying {provider_name}..."
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
            errors.append(str(exc))

            if exc.status_code == 429:
                # A provider that repeatedly returns 429 should not be pinged
                # again on every candidate. Honor longer provider hints, but
                # enforce a 30s minimum in-run cooldown to reduce retry churn.
                delay = max(
                    30.0,
                    _provider_retry_delay(str(exc)),
                )

                VISION_PROVIDER_COOLDOWN_UNTIL[
                    provider_id
                ] = time.monotonic() + delay

                print(
                    f"Visual verifier {provider_name} rate-limited; "
                    f"cooling this route for {delay:.0f}s and switching backup."
                )

            elif exc.status_code in (
                400, 401, 403, 404, 413, 415, 422
            ):
                VISION_DISABLED_PROVIDERS.add(
                    provider_id
                )

                print(
                    f"Visual verifier {provider_name} has a hard "
                    f"configuration error (HTTP {exc.status_code}); "
                    "disabling this route."
                )

            else:
                VISION_PROVIDER_COOLDOWN_UNTIL[
                    provider_id
                ] = time.monotonic() + 20

                print(
                    f"Visual verifier {provider_name} unavailable: "
                    f"{str(exc)[:450]}"
                )

        except VisionResponseError as exc:
            errors.append(str(exc))

            VISION_DISABLED_PROVIDERS.add(
                provider_id
            )

            print(
                f"Visual verifier {provider_name} returned unusable output: "
                f"{str(exc)[:450]}. Disabling this route."
            )

        except requests.exceptions.Timeout as exc:
            errors.append(
                f"{provider_name}: timeout: {exc}"
            )

            VISION_PROVIDER_COOLDOWN_UNTIL[
                provider_id
            ] = time.monotonic() + 30

            print(
                f"Visual verifier {provider_name} timed out; "
                "switching backup."
            )

        except Exception as exc:
            errors.append(
                f"{provider_name}: {exc}"
            )

            VISION_PROVIDER_COOLDOWN_UNTIL[
                provider_id
            ] = time.monotonic() + 20

            print(
                f"Visual verifier {provider_name} failed: "
                f"{str(exc)[:450]}"
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
    """Rank by relevance inside strict source tiers, with unknown sources last."""
    candidates_by_source = {}

    for candidate_item in candidates:
        candidate_source = str(
            candidate_item[1].get("source") or ""
        ).strip()
        candidates_by_source.setdefault(
            candidate_source, []
        ).append(candidate_item)

    ranked = []

    for candidate_source in FOOTAGE_SOURCE_PRIORITY:
        source_candidates = sorted(
            candidates_by_source.pop(candidate_source, []),
            key=lambda item: item[0],
            reverse=True
        )
        source_limit = FOOTAGE_SOURCE_CANDIDATE_LIMITS[candidate_source]
        ranked.extend(source_candidates[:source_limit])

    # Unknown future sources are fallback-only and remain after all known tiers.
    for candidate_source in sorted(candidates_by_source):
        source_candidates = sorted(
            candidates_by_source[candidate_source],
            key=lambda item: item[0],
            reverse=True
        )
        ranked.extend(source_candidates[:4])

    return ranked


def fetch_footage(plan):
    credits = []
    used_ids = set()
    selected_tag_sets = []
    verified_assets = []

    visual_provider_names = []

    if GROQ_KEY:
        visual_provider_names.append(
            f"Groq ({GROQ_VISION_MODEL})"
        )

    if OPENROUTER_KEY:
        visual_provider_names.append(
            f"OpenRouter ({OPENROUTER_VISION_MODEL})"
        )

        if (
            OPENROUTER_VISION_FALLBACK_MODEL
            != OPENROUTER_VISION_MODEL
        ):
            visual_provider_names.append(
                "OpenRouter fallback "
                f"({OPENROUTER_VISION_FALLBACK_MODEL})"
            )

    if GEMINI_KEY:
        visual_provider_names.append(
            f"Gemini ({VISION_MODEL})"
        )

    if VISION_REQUIRED and not visual_provider_names:
        raise RuntimeError(
            "Stage 1 requires visual verification. Add at least one of "
            "GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY under "
            "GitHub Settings > Secrets and variables > Actions."
        )

    if not PEXELS_KEY:
        print(
            "Footage source: Pexels disabled because "
            "PEXELS_API_KEY is not configured."
        )

    print(
        "Footage selection priority (strict tiers): "
        + " -> ".join(FOOTAGE_SOURCE_PRIORITY)
        + ". Relevance is ranked only within each source."
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

        candidates = {}

        print(
            f"Scene {i}: applying metadata anchors independently to "
            f"each of {len(queries)} search variants."
        )

        def collect_query_candidates(query):
            for hit in search_all_footage_sources(query):
                identity = f"{hit.get('source')}:{hit.get('id')}"

                if not hit.get("url") or identity in used_ids:
                    continue

                if not candidate_matches_visual_anchors(
                    hit, query, primary_query=scene.get("visual_query")
                ):
                    continue

                width = int(hit.get("width") or 0)
                height = int(hit.get("height") or 0)
                duration = float(hit.get("duration") or 0)
                if width and width < MIN_CLIP_WIDTH:
                    continue
                if width and height and not is_native_portrait_916(width, height):
                    continue
                if duration and duration < 4:
                    continue

                score, matched, total = relevance_score(
                    hit, query, scene.get("narration", "")
                )
                if matched < 1 or score < 0.20:
                    continue

                metadata_tokens = set(normalize_words(
                    str(hit.get("metadata", "")) + " " + str(hit.get("tags", ""))
                ))
                scene_context = set(normalize_words(scene.get("narration", "")))

                if any(
                    metadata_tokens & conflict_terms
                    and not scene_context & conflict_terms
                    for conflict_terms in CONTEXT_CONFLICTS.values()
                ):
                    continue

                tags = str(hit.get("tags") or hit.get("metadata") or "")
                similarity = max(
                    [tag_similarity(tags, old) for old in selected_tag_sets] or [0.0]
                )
                if similarity > 0.78:
                    continue

                previous = candidates.get(identity)
                if previous is None or score > previous[0]:
                    candidates[identity] = (score, hit, query, matched, total)

        for query in queries:
            collect_query_candidates(query)

        # Only genuinely sparse selectable pools pay for the larger fallback
        # search wave. Measure the source-capped shortlist, not the raw metadata
        # pool: dozens of hits can still collapse to a handful after tier caps.
        ranked = rank_footage_candidates_by_source(candidates.values())
        targeted_search_threshold = min(12, VISION_CANDIDATE_LIMIT)
        if len(ranked) < targeted_search_threshold:
            extra_queries = fallback_visual_queries(
                raw_queries,
                scene.get("narration", ""),
            )
            existing = {query.lower() for query in queries}
            extra_queries = [q for q in extra_queries if q.lower() not in existing]

            if extra_queries:
                print(
                    f"Scene {i}: only {len(ranked)} selectable candidates "
                    f"after source-tier limits (target: {targeted_search_threshold}); "
                    f"trying {len(extra_queries)} "
                    "targeted fallback searches."
                )

            for query in extra_queries:
                queries.append(query)
                collect_query_candidates(query)
                ranked = rank_footage_candidates_by_source(candidates.values())
                if len(ranked) >= VISION_CANDIDATE_LIMIT:
                    break

        # Never compare scores across different providers. Try the preferred
        # source tier first; lower tiers are reached only when earlier candidates
        # fail to download or fail visual verification.
        ranked = rank_footage_candidates_by_source(candidates.values())

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

        for (
            score,
            hit,
            query,
            matched,
            total
        ) in ranked[:VISION_CANDIDATE_LIMIT]:

            candidate_source = str(hit.get("source") or "").strip()
            cooldown_until = FOOTAGE_SOURCE_COOLDOWN_UNTIL.get(
                candidate_source, 0.0
            )
            if time.monotonic() < cooldown_until:
                remaining = max(1, int(cooldown_until - time.monotonic()))
                print(
                    f"Scene {i}: skipping remaining {candidate_source} "
                    f"candidates during its {remaining}s rate-limit cooldown."
                )
                continue

            checked += 1

            try:
                download_stream(
                    hit["url"],
                    destination,
                    source=candidate_source,
                )

                source_video_info = ffprobe_video_info(destination)
                if not is_native_portrait_916(
                    source_video_info["width"], source_video_info["height"],
                    aspect_ratio=source_video_info["aspect_ratio"],
                ):
                    raise RuntimeError(
                        "Source footage rejected: native 9:16 portrait is required; "
                        f"downloaded source is {source_video_info['width']}x"
                        f"{source_video_info['height']} "
                        f"(aspect {source_video_info['aspect_ratio']:.4f}). "
                        "The generator will not crop landscape, square, or 4:5 footage."
                    )
                hit["_source_video_info"] = source_video_info

                if visual_provider_names:
                    candidate_metadata = " | ".join([
                        str(hit.get("metadata", "")),
                        str(hit.get("tags", "")),
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
                        destination.unlink(
                            missing_ok=True
                        )
                        continue

                else:
                    confidence = None
                    vision_provider = "Metadata-only"
                    visual_reason = (
                        "Metadata-only fallback explicitly enabled."
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

                print(
                    f"Scene {i}: candidate rejected/skipped "
                    f"({hit.get('source')}): {message[:350]}"
                )

                destination.unlink(
                    missing_ok=True
                )

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
        scene["source_video_dimensions"] = hit.get("_source_video_info", {})
        scene["source_native_aspect_ratio_required"] = "9:16"

        scene["visual_verification"] = {
            "method": (
                f"{vision_provider} Vision"
                if vision_confidence is not None
                else "metadata-only fallback"
            ),
            "confidence": vision_confidence,
            "reason": vision_reason,
            "query": query,
        }

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

        credits.append(
            f"Scene {i} | {hit.get('source')} | "
            f"Creator: {hit.get('creator', 'Unknown')} | "
            f"Source: {hit.get('page_url', '')} | "
            f"Metadata relevance: {score:.2f} "
            f"({matched}/{total} query concepts)"
            f"{vision_credit} | "
            f"License: {hit.get('license', 'Check source page')}"
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

def build_voice_and_subs(script):
    """Sentence-level TTS with deliberate pauses and word timing."""
    sentences = split_sentences(script)

    if not sentences:
        raise RuntimeError(
            "The script contains no speakable sentences."
        )

    async def generate():
        word_events = []
        sentence_word_events = []
        sentence_events = []
        wav_parts = []
        cursor = 0.0

        opening_silence = OUT / "opening_silence.wav"

        run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(OPENING_LEAD),
            "-c:a", "pcm_s16le",
            str(opening_silence)
        ])

        wav_parts.append(opening_silence)
        cursor += OPENING_LEAD

        for index, sentence in enumerate(sentences):
            rate_values = [
                BASE_RATE, "-5%", "-3%", "-4%"
            ]

            pitch_values = [
                BASE_PITCH, "-2Hz", "-4Hz", "-2Hz"
            ]

            mp3_path = OUT / f"tts_{index:02d}.mp3"
            wav_path = OUT / f"tts_{index:02d}.wav"

            boundaries = []

            communicate = edge_tts.Communicate(
                sentence,
                VOICE,
                rate=rate_values[
                    index % len(rate_values)
                ],
                pitch=pitch_values[
                    index % len(pitch_values)
                ],
                boundary="WordBoundary"
            )

            with open(mp3_path, "wb") as f:
                async for message in communicate.stream():
                    if message["type"] == "audio":
                        f.write(message["data"])

                    elif message["type"] == "WordBoundary":
                        boundaries.append({
                            "text": message["text"],
                            "offset": (
                                message["offset"] / 10_000_000
                            ),
                            "duration": (
                                message["duration"] / 10_000_000
                            )
                        })

            if (
                not mp3_path.exists()
                or mp3_path.stat().st_size < 1000
                or not boundaries
            ):
                raise RuntimeError(
                    f"Edge TTS failed on sentence {index+1}."
                )

            run([
                "ffmpeg", "-y",
                "-i", str(mp3_path),
                "-ar", "48000",
                "-ac", "2",
                "-c:a", "pcm_s16le",
                str(wav_path)
            ])

            duration = ffprobe_duration(wav_path)
            sentence_start = cursor

            timed_sentence_words = []
            for event in boundaries:
                timed_event = {
                    **event,
                    "offset": cursor + event["offset"]
                }
                word_events.append(timed_event)
                timed_sentence_words.append(timed_event)

            # TTS word-boundary payloads may omit punctuation. Restore the
            # original sentence ending without changing the audio timing.
            if timed_sentence_words:
                terminal = re.search(r"([.!?][\"']?)$", sentence.strip())
                last_text = str(timed_sentence_words[-1].get("text") or "")
                if terminal and not re.search(r"[.!?][\"']?$", last_text):
                    timed_sentence_words[-1]["text"] = last_text + terminal.group(1)
            sentence_word_events.append(timed_sentence_words)

            wav_parts.append(wav_path)
            cursor += duration

            sentence_events.append({
                "start": sentence_start,
                "end": cursor
            })

            gap = (
                HOOK_GAP
                if index == 0
                else SENTENCE_GAP
            )

            silence = OUT / f"gap_{index:02d}.wav"

            run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", "anullsrc=r=48000:cl=stereo",
                "-t", str(gap),
                "-c:a", "pcm_s16le",
                str(silence)
            ])

            wav_parts.append(silence)
            cursor += gap

        last_gap = wav_parts.pop()
        last_gap.unlink(missing_ok=True)

        ending = OUT / "ending_hold.wav"

        run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(ENDING_HOLD),
            "-c:a", "pcm_s16le",
            str(ending)
        ])

        wav_parts.append(ending)

        total_duration = sum(
            ffprobe_duration(p)
            for p in wav_parts
        )

        concat = OUT / "voice_concat.txt"

        concat.write_text(
            "".join(
                f"file '{p.resolve()}'\n"
                for p in wav_parts
            ),
            encoding="utf-8"
        )

        run([
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat),
            "-c:a", "pcm_s16le",
            "-ar", "48000",
            "-ac", "2",
            str(OUT / "voiceover.wav")
        ])

        run([
            "ffmpeg", "-y",
            "-i", str(OUT / "voiceover.wav"),
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(OUT / "voiceover.mp3")
        ])

        lines = [
            "[Script Info]",
            "Title: Consistent Sentence-Bounded Documentary Captions",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Default,DejaVu Sans,52,&H00FFFFFF,&H000000FF,&H00101A25,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,2,90,90,360,1",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
        ]

        for sentence_group in sentence_word_events:
            for group in build_caption_groups(sentence_group):
                start = group[0]["offset"]
                end = max(
                    group[-1]["offset"] + group[-1]["duration"],
                    start + 0.45,
                )
                phrase = " ".join(str(x["text"]) for x in group)
                phrase = r"\N".join(
                    textwrap.wrap(
                        phrase,
                        width=CAPTION_MAX_CHARS,
                        break_long_words=False,
                        break_on_hyphens=False,
                    )
                )
                phrase = phrase.replace("{", r"\{").replace("}", r"\}")
                lines.append(
                    f"Dialogue: 0,{ass_time(start)},"
                    f"{ass_time(end)},Default,,0,0,0,,{phrase}"
                )

        (OUT / "captions.ass").write_text(
            "\n".join(lines),
            encoding="utf-8"
        )

        Path("timing.json").write_text(
            json.dumps({
                "sentences": sentence_events,
                "total_duration": total_duration
            }, indent=2),
            encoding="utf-8"
        )

        return sentence_events, total_duration

    return asyncio.run(generate())


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
    hook_text=None
):
    source_info = ffprobe_video_info(source)
    if not is_native_portrait_916(
        source_info["width"], source_info["height"],
        aspect_ratio=source_info["aspect_ratio"],
    ):
        raise RuntimeError(
            "Render refused non-native-9:16 footage: "
            f"{source_info['width']}x{source_info['height']} "
            f"(aspect {source_info['aspect_ratio']:.4f})."
        )

    source_duration = ffprobe_duration(source)

    loop_args = (
        ["-stream_loop", "-1"]
        if source_duration < duration + 0.1
        else []
    )

    # The downloaded source has been checked as native 9:16. Fit the complete
    # frame into the canvas; never enlarge-and-crop or apply artificial panning.
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={FPS},setsar=1,format=yuv420p"
    )

    if hook_text:
        hook_file = OUT / "hook.txt"
        wrapped_hook = wrap_overlay_text(hook_text, width=24, max_lines=5)
        hook_file.write_text(wrapped_hook, encoding="utf-8")
        longest_hook_line = max(
            (len(line) for line in wrapped_hook.splitlines()),
            default=1,
        )
        hook_font_size = max(
            32,
            min(54, int((W - 180) / max(1, longest_hook_line * 0.62))),
        )

        vf += (
            f",drawtext=fontfile=/usr/share/fonts/truetype/dejavu/"
            f"DejaVuSans-Bold.ttf:textfile={hook_file}:"
            f"fontcolor=white:fontsize={hook_font_size}:line_spacing=8:"
            "borderw=2:bordercolor=black@0.70:"
            "box=1:boxcolor=black@0.42:boxborderw=30:"
            "x=(w-text_w)/2:y=160:"
            "alpha='if(lt(t,1.0),(t-0.6)/0.4,"
            "if(gt(t,4.1),(4.5-t)/0.4,1))':"
            "enable='between(t,0.6,4.5)'"
        )

    cmd = [
        "ffmpeg", "-y",
        *loop_args,
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", X264_PRESET,
        "-crf", "20",
        "-r", str(FPS),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output)
    ]

    run(cmd)


def render(plan, sentence_events, total_duration):
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
            hook = None

            if i == 1:
                hook = re.sub(
                    r"[^A-Za-z0-9 ,.!?\-]",
                    "",
                    str(plan.get("hook") or "")
                ).strip()

            render_footage_scene(
                source,
                duration,
                output,
                hook
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

    audio_filter = (
        "[1:a]highpass=f=65,lowpass=f=15000,"
        "acompressor=threshold=-22dB:ratio=2.0:"
        "attack=25:release=250:makeup=1.15,"
        "alimiter=limit=0.88,"
        "loudnorm=I=-16:TP=-1.5:LRA=9,"
        "aresample=48000[a]"
    )

    run([
        "ffmpeg", "-y",
        "-i", str(base),
        "-i", str(OUT / "voiceover.wav"),
        "-filter_complex",
        f"[0:v]subtitles={OUT}/captions.ass[v];{audio_filter}",
        "-map", "[v]",
        "-map", "[a]",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264",
        "-preset", X264_PRESET,
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(final)
    ])

    print(
        f"Final video: {final}; "
        f"duration {ffprobe_duration(final):.2f}s"
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
            "visual_verification_required": VISION_REQUIRED,
            "groq_key_configured": bool(GROQ_KEY),
            "openrouter_key_configured": bool(OPENROUTER_KEY),
            "gemini_key_configured": bool(GEMINI_KEY),
            "pixabay_key_configured": bool(PIXABAY_KEY),
            "pexels_key_configured": bool(PEXELS_KEY),
            "source_priority": list(FOOTAGE_SOURCE_PRIORITY),
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


def validate_runtime_config():
    if not (GROQ_KEY or OPENROUTER_KEY or GEMINI_KEY):
        raise RuntimeError(
            "Missing AI provider key. Add GROQ_API_KEY, "
            "OPENROUTER_API_KEY, or GEMINI_API_KEY GitHub Secret."
        )
    if not TOPIC:
        raise RuntimeError("Enter a topic in the Run workflow form.")


def main():
    validate_runtime_config()

    start_pipeline_stage("documentary_plan_generation")
    print("1/5 Generating documentary plan...")
    plan = gemini_plan(TOPIC)
    finish_pipeline_stage()

    start_pipeline_stage("footage_search_download_and_visual_verification")
    print("2/5 Searching and ranking relevant footage...")
    fetch_footage(plan)
    finish_pipeline_stage()

    start_pipeline_stage("voiceover_and_subtitle_generation")
    print("3/5 Generating paced voice-over and subtitles...")
    sentence_events, total_duration = build_voice_and_subs(plan["script"])
    finish_pipeline_stage()

    start_pipeline_stage("video_rendering")
    print("4/5 Rendering scenes to actual narration timing...")
    render(plan, sentence_events, total_duration)
    finish_pipeline_stage()

    PIPELINE_STATE["status"] = "completed"
    PIPELINE_STATE["current_stage"] = "complete"
    _write_json_report("stage_report.json", PIPELINE_STATE)
    print(
        "5/5 Production complete: "
        "documentary.mp4, plan.json, credits.txt, timing.json"
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
            sys.stdout = original_stdout
            sys.stderr = original_stderr


if __name__ == "__main__":
    run_with_diagnostics()

