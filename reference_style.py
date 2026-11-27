"""MM6 presentation layer for the reference-style vertical documentary.

The reference channel (Alam Semenit) publishes Indonesian short documentaries
with a bold, readable subtitle treatment, a calm but engaging narration and a
continuous music bed. This module owns the parts of that presentation that are
independent from provider/API plumbing:

* language profiles (script language, TTS voice, caption casing);
* reference-style caption typography for the 1080x1920 canvas;
* a 100% royalty-free music bed synthesised locally with FFmpeg (no third-party
  audio is downloaded, so the final video carries no music copyright risk);
* a copyright/compliance report written next to ``credits.txt``.

Everything here is pure Python plus an FFmpeg call for the music bed, so it can
be unit tested without any API credentials.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# ------------------------------------------------------------
# LANGUAGE PROFILES
# ------------------------------------------------------------

LANGUAGE_PROFILES = {
    "id": {
        "code": "id",
        "name": "Indonesian",
        "native_name": "Bahasa Indonesia",
        "edge_voice": "id-ID-ArdiNeural",
        "edge_voice_fallback": "id-ID-GadisNeural",
        "speechify_locale": "id-ID",
        # Bold uppercase subtitles are the norm for Indonesian vertical
        # documentaries and read well on a phone at arm's length.
        "caption_uppercase": True,
        "caption_max_words": 4,
        "caption_max_chars": 26,
        "caption_font_size": 66,
        "caption_margin_v": 420,
        "caption_outline": 4,
    },
    "en": {
        "code": "en",
        "name": "English",
        "native_name": "English",
        "edge_voice": "en-US-GuyNeural",
        "edge_voice_fallback": "en-US-AndrewNeural",
        "speechify_locale": "en-US",
        "caption_uppercase": False,
        "caption_max_words": 5,
        "caption_max_chars": 32,
        "caption_font_size": 60,
        "caption_margin_v": 400,
        "caption_outline": 3,
    },
}

DEFAULT_LANGUAGE = "id"

# Common function words used only to detect an obvious language mismatch
# (for example an English script returned for an Indonesian request).
_LANGUAGE_STOPWORDS = {
    "id": {
        "yang", "dan", "di", "ini", "itu", "dengan", "untuk", "dari", "pada",
        "adalah", "tidak", "akan", "bisa", "juga", "karena", "lebih", "saat",
        "dalam", "seperti", "tetapi", "namun", "hanya", "sudah", "masih",
        "mereka", "kita", "kami", "banyak", "sangat", "antara", "hingga",
        "setiap", "bahkan", "jika", "ketika", "sementara", "selama", "tanpa",
        "punya", "membuat", "menjadi", "ada", "oleh", "para", "satu", "dua",
    },
    "en": {
        "the", "and", "of", "to", "in", "is", "that", "it", "with", "for",
        "as", "was", "on", "are", "this", "be", "by", "from", "or", "an",
        "which", "but", "not", "they", "their", "have", "has", "at", "its",
        "these", "would", "can", "when", "more", "than", "into", "about",
    },
}


def resolve_language_code(value: str | None) -> str:
    """Map an environment value to a supported two-letter language code."""
    raw = str(value or "").strip().lower()
    if not raw:
        return DEFAULT_LANGUAGE
    if raw in LANGUAGE_PROFILES:
        return raw
    # Accept locale-style values such as ``id-ID`` or ``in_ID``.
    primary = re.split(r"[-_]", raw)[0]
    if primary in LANGUAGE_PROFILES:
        return primary
    if primary == "in":  # legacy ISO code for Indonesian
        return "id"
    return DEFAULT_LANGUAGE


def language_profile(code: str | None = None) -> dict:
    """Return a copy of the profile for ``code`` (defaults to Indonesian)."""
    resolved = resolve_language_code(code)
    profile = dict(LANGUAGE_PROFILES[resolved])
    profile["code"] = resolved
    return profile


def language_display_name(code: str | None = None) -> str:
    return language_profile(code)["name"]


def narration_language_matches(script: str, code: str | None = None) -> bool:
    """Detect an obvious wrong-language response from a provider.

    The check is intentionally lenient: it only fails when the requested
    language's function words are essentially absent while the other supported
    language's function words clearly dominate. Named entities, quotations and
    technical terms must never trigger a false failure.
    """
    requested = resolve_language_code(code)
    words = re.findall(r"[a-z']+", str(script or "").lower())
    if len(words) < 20:
        return True

    def hits(language: str) -> int:
        stopwords = _LANGUAGE_STOPWORDS[language]
        return sum(1 for word in words if word in stopwords)

    own_hits = hits(requested)
    other_hits = sum(hits(other) for other in _LANGUAGE_STOPWORDS if other != requested)
    own_ratio = own_hits / len(words)
    other_ratio = other_hits / len(words)

    if own_ratio >= 0.04:
        return True
    if other_ratio >= 0.10 and other_ratio > own_ratio * 2:
        return False
    return True


# ------------------------------------------------------------
# CAPTIONS
# ------------------------------------------------------------

REFERENCE_CAPTION_TITLE = "Reference-style vertical documentary captions"


def caption_style(code: str | None = None) -> dict:
    """Subtitle geometry/typography for the reference look."""
    profile = language_profile(code)
    return {
        "uppercase": bool(profile["caption_uppercase"]),
        "max_words": int(profile["caption_max_words"]),
        "max_chars": int(profile["caption_max_chars"]),
        "font_size": int(profile["caption_font_size"]),
        "margin_v": int(profile["caption_margin_v"]),
        "outline": int(profile["caption_outline"]),
    }


def format_caption_text(phrase: str, uppercase: bool = True) -> str:
    """Normalise whitespace and optionally upper-case a caption phrase."""
    clean = re.sub(r"\s+", " ", str(phrase or "")).strip()
    if not clean:
        return ""
    return clean.upper() if uppercase else clean


HOOK_STYLE_NAME = "Hook"


def hook_style_line(style: dict) -> str:
    """Large top-centred opening title style (same font as subtitles)."""
    font_size = max(40, int(style.get("font_size", 66)) - 4)
    return (
        f"Style: {HOOK_STYLE_NAME},DejaVu Sans,{font_size},"
        "&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,1,0,1,4,1,8,60,60,120,1"
    )


def format_hook_event(hook: str, style: dict, start=0.5, end=5.4) -> str:
    """ASS dialogue for the opening hook, wrapped and optionally upper-cased."""
    text = format_caption_text(hook, uppercase=bool(style.get("uppercase")))
    if not text:
        return ""
    import textwrap

    wrapped = textwrap.wrap(text, width=22, break_long_words=False, break_on_hyphens=False)
    if len(wrapped) > 5:
        wrapped = textwrap.wrap(text, width=26, break_long_words=False, break_on_hyphens=False)
    joined = r"\N".join(wrapped)
    joined = joined.replace("{", r"\{").replace("}", r"\}")
    return (
        f"Dialogue: 1,{ass_clock(start)},{ass_clock(end)},"
        f"{HOOK_STYLE_NAME},,0,0,0,,{joined}"
    )


def ass_clock(seconds: float) -> str:
    ticks = max(0, int(round(float(seconds) * 100)))
    return (
        f"{ticks // 360000}:{(ticks // 6000) % 60:02}:{(ticks // 100) % 60:02}."
        f"{ticks % 100:02}"
    )


def append_hook_caption(captions_path, hook: str, style: dict, start=0.5, end=5.4):
    """Insert the hook title into the ASS file so rendering needs no drawtext.

    Using libass instead of FFmpeg's drawtext keeps the hook working on builds
    without the freetype text filter and guarantees one consistent font stack
    for both the hook and the subtitles.
    """
    path = Path(captions_path)
    if not path.is_file():
        return None
    hook_event = format_hook_event(hook, style, start=start, end=end)
    if not hook_event:
        return path
    lines = path.read_text(encoding="utf-8").splitlines()
    # Register the Hook style right after the Default style line.
    for index, line in enumerate(lines):
        if line.startswith("Style: Default,"):
            lines.insert(index + 1, hook_style_line(style))
            break
    lines.append(hook_event)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def caption_ass_header(style: dict, title: str = REFERENCE_CAPTION_TITLE) -> list:
    """Build the ASS header for the 1080x1920 canvas.

    The style is a bold, heavily outlined, centred subtitle block placed above
    the Facebook Reels UI zone (bottom ~400 px are covered by the app shell).
    """
    font_size = int(style.get("font_size", 66))
    margin_v = int(style.get("margin_v", 420))
    outline = int(style.get("outline", 4))
    # Alignment 2 = bottom-centre. Primary white, black outline, soft shadow,
    # fully opaque box disabled so the footage stays visible.
    style_line = (
        "Style: Default,DejaVu Sans,"
        f"{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,1,0,1,{outline},1,2,"
        f"70,70,{margin_v},1"
    )
    return [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        style_line,
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]


# ------------------------------------------------------------
# ROYALTY-FREE MUSIC BED
# ------------------------------------------------------------

MUSIC_BED_PROVENANCE = (
    "Synthesised locally by FFmpeg from pure tones (aevalsrc). No samples, "
    "recordings or third-party tracks are used, so the bed is original and "
    "free of music copyright claims on Facebook/Reels."
)


def music_bed_available(ffmpeg_binary: str = "ffmpeg") -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return "aevalsrc" in (result.stdout or "")


def music_bed_expression(channel: int = 0) -> str:
    """Slow ambient pad: minor add9 chord with detuned stereo width.

    A2 + E3 + A3 + C4 plus a sub drone, each with an independent slow swell so
    the pad breathes instead of sounding like a static test tone.
    """
    detune = 0.35 * channel
    phase = 0.6 * channel
    return (
        f"0.20*sin(2*PI*({110.00 + detune})*t)*(0.6+0.4*sin(2*PI*0.043*t+{phase}))"
        f"+0.13*sin(2*PI*({164.81 + detune})*t)*(0.65+0.35*sin(2*PI*0.031*t+{1.2 + phase}))"
        f"+0.10*sin(2*PI*({220.00 + detune})*t)*(0.7+0.3*sin(2*PI*0.027*t+{2.4 + phase}))"
        f"+0.07*sin(2*PI*({261.63 + detune})*t)*(0.7+0.3*sin(2*PI*0.021*t+{0.9 + phase}))"
        f"+0.10*sin(2*PI*({55.00 + detune / 2})*t)*(0.8+0.2*sin(2*PI*0.017*t))"
    )


def music_bed_filter(duration: float) -> str:
    """Filter chain applied to the synthesised pad."""
    fade = min(2.0, max(0.5, float(duration) / 12.0))
    return (
        "highpass=f=38,lowpass=f=1100,"
        # tremolo requires f >= 0.1 Hz; 0.12 Hz is a very slow, breathing swell.
        "tremolo=f=0.12:d=0.18,"
        "aecho=0.8:0.88:1100|1700:0.22|0.16,"
        f"afade=t=in:st=0:d={fade:.2f},"
        f"afade=t=out:st={max(0.0, float(duration) - fade):.2f}:d={fade:.2f}"
    )


SFX_PROVENANCE = (
    "Transition whooshes and impacts are synthesised locally by FFmpeg "
    "(filtered noise / decaying sine). They are original sounds with no "
    "third-party sample licence."
)


def build_whoosh(output_path, run_command=None, ffmpeg_binary="ffmpeg", duration=0.55):
    """Short filtered-noise transition whoosh, generated locally."""
    duration = max(0.25, float(duration))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fade_out_start = max(0.05, duration - 0.28)
    command = [
        ffmpeg_binary, "-y",
        "-f", "lavfi",
        "-i", f"anoisesrc=d={duration:.3f}:c=pink:r=48000:a=0.35",
        "-af", (
            "highpass=f=420,lowpass=f=5200,tremolo=f=5:d=0.35,"
            "aecho=0.65:0.55:70:0.28,"
            f"afade=t=in:st=0:d=0.16,afade=t=out:st={fade_out_start:.3f}:d=0.28,"
            "volume=0.55"
        ),
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(output),
    ]
    if run_command is not None:
        run_command(command)
    else:
        subprocess.run(command, check=True, capture_output=True, text=True)
    return output


def build_impact(output_path, run_command=None, ffmpeg_binary="ffmpeg", duration=0.9):
    """Low decaying impact used under the hook. Generated locally."""
    duration = max(0.3, float(duration))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    expr = (
        "0.75*sin(2*PI*52*t)*exp(-6.5*t)"
        "+0.32*sin(2*PI*104*t)*exp(-10*t)"
        "+0.18*sin(2*PI*39*t)*exp(-4*t)"
    )
    command = [
        ffmpeg_binary, "-y",
        "-f", "lavfi",
        "-i", f"aevalsrc={expr}:s=48000:d={duration:.3f}",
        "-af", "lowpass=f=260,afade=t=out:st=0.35:d=" + f"{max(0.1, duration - 0.35):.3f}",
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(output),
    ]
    if run_command is not None:
        run_command(command)
    else:
        subprocess.run(command, check=True, capture_output=True, text=True)
    return output


def build_music_bed(duration: float, output_path, run_command=None, ffmpeg_binary="ffmpeg"):
    """Render the royalty-free ambient bed to ``output_path``.

    ``run_command`` is injected by main.py so FFmpeg failures go through the
    pipeline's normal diagnostics. Returns the output path.
    """
    duration = max(0.5, float(duration))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    left = music_bed_expression(0)
    right = music_bed_expression(1)
    command = [
        ffmpeg_binary, "-y",
        "-f", "lavfi",
        "-i", f"aevalsrc={left}|{right}:s=48000:d={duration:.3f}",
        "-t", f"{duration:.3f}",
        "-af", music_bed_filter(duration),
        "-ac", "2", "-ar", "48000",
        "-c:a", "pcm_s16le",
        str(output),
    ]
    if run_command is not None:
        run_command(command)
    else:
        subprocess.run(command, check=True, capture_output=True, text=True)
    return output


# ------------------------------------------------------------
# COPYRIGHT / COMPLIANCE REPORT
# ------------------------------------------------------------

def build_copyright_report(
    plan: dict,
    language_code: str,
    voice_provider: str,
    music_provenance: str | None = None,
) -> dict:
    """Summarise every reusable component of the finished video."""
    scenes = []
    for index, scene in enumerate((plan or {}).get("scenes", []), 1):
        scenes.append({
            "scene": index,
            "source": scene.get("source") or "unknown",
            "creator": scene.get("creator") or "unknown",
            "page_url": scene.get("page_url") or "",
            "license": scene.get("license") or "check source page",
            "attribution_required": bool(scene.get("coverr_attribution")),
        })
    return {
        "video_format": {
            "resolution": "1080x1920",
            "aspect_ratio": "9:16",
            "target_platform": "Facebook Reels",
        },
        "narration_language": language_code,
        "voice_over": {
            "provider": voice_provider,
            "license_note": (
                "Narration is synthesised speech generated for this run from the "
                "script; no third-party recording is used."
            ),
        },
        "music": {
            "enabled": bool(music_provenance),
            "provenance": music_provenance or "No music bed used.",
        },
        "footage": scenes,
        "font": {
            "family": "DejaVu Sans",
            "license": "DejaVu Fonts License (free for commercial use)",
        },
        "publication_note": (
            "Verify every footage licence on the source page before publishing. "
            "Creative Commons BY/BY-SA clips require creator attribution; it is "
            "written to credits.txt. Coverr free-tier clips require Coverr and "
            "creator attribution."
        ),
    }


def write_copyright_report(path, payload: dict) -> Path:
    target = Path(path)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target
