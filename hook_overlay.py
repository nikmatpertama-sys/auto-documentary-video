"""Pixel-measured, Unicode-safe opening title/hook overlay."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

BUNDLED_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def _font_candidates(bold: bool) -> list[Path]:
    """Return every usable TrueType path, most portable source first.

    The DejaVu faces are bundled with the repository so the overlay never
    silently degrades to Pillow's tiny bitmap default on a runner or container
    that forgot to install system fonts (that fallback produced unreadable
    title cards).
    """
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates: list[Path] = []

    configured = os.getenv(
        "HOOK_FONT_BOLD_PATH" if bold else "HOOK_FONT_PATH", ""
    ).strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    candidates.append(BUNDLED_FONT_DIR / filename)
    candidates.extend([
        Path("/usr/share/fonts/truetype/dejavu") / filename,
        Path("/usr/share/fonts/TTF") / filename,
        Path("/usr/share/fonts/dejavu") / filename,
        Path("/usr/local/share/fonts") / filename,
    ])
    candidates.append(
        Path("/usr/share/fonts/truetype/liberation2")
        / ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf")
    )
    candidates.append(
        Path("/usr/share/fonts/truetype/liberation")
        / ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf")
    )
    return candidates


@lru_cache(maxsize=64)
def _font_path(bold: bool) -> str:
    for candidate in _font_candidates(bold):
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(
        "No TrueType font is available for the opening overlay. Install "
        "fonts-dejavu-core or set HOOK_FONT_PATH/HOOK_FONT_BOLD_PATH; a bitmap "
        "fallback would produce an unreadable title card."
    )


def _load_font(size: int, bold: bool = True):
    return ImageFont.truetype(_font_path(bold), size=size)


def _wrap_by_pixels(text: str, font, draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_lines(text: str, draw: ImageDraw.ImageDraw, max_width: int, max_lines: int,
               start_size: int, min_size: int = 34) -> tuple[object, list[str]]:
    for size in range(start_size, min_size - 1, -2):
        font = _load_font(size, bold=True)
        lines = _wrap_by_pixels(text, font, draw, max_width)
        if len(lines) <= max_lines and all(
            draw.textbbox((0, 0), line, font=font)[2] <= max_width for line in lines
        ):
            return font, lines
    # Preserve every word even when the preferred line count cannot be met.
    # The caller sizes the card from the actual resulting line count, so no
    # right-edge clipping or silent text truncation is needed.
    font = _load_font(min_size, bold=True)
    lines = _wrap_by_pixels(text, font, draw, max_width)
    return font, lines


def create_hook_overlay(title: str, hook: str, output: str | Path,
                        width: int = 1080, height: int = 1920) -> dict:
    """Write a transparent PNG with a safely wrapped title and hook.

    Unicode punctuation is preserved. Text is measured in actual pixels, not
    estimated with character counts, so long hooks cannot run off-screen.
    """
    title = " ".join(str(title or "").replace("\n", " ").split()).strip()
    hook = " ".join(str(hook or "").replace("\n", " ").split()).strip()
    if not hook:
        raise ValueError("Opening hook cannot be empty.")

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    card_x = 48
    card_w = width - 96
    text_w = card_w - 104
    title_font = None
    title_lines: list[str] = []
    if title:
        title_font, title_lines = _fit_lines(
            title.upper(), draw, text_w, max_lines=3, start_size=27, min_size=18
        )

    hook_font, hook_lines = _fit_lines(
        hook, draw, text_w, max_lines=5, start_size=62, min_size=32
    )
    if not hook_lines:
        raise ValueError("Opening hook did not produce any visible text.")

    title_line_h = 0
    if title_font:
        title_line_h = max(1, title_font.getbbox("Ag")[3] - title_font.getbbox("Ag")[1])
    hook_line_h = max(1, hook_font.getbbox("Ag")[3] - hook_font.getbbox("Ag")[1])
    title_block_h = len(title_lines) * title_line_h + (8 if title_lines else 0)
    hook_block_h = len(hook_lines) * hook_line_h + max(0, len(hook_lines) - 1) * 10
    content_h = title_block_h + hook_block_h + (16 if title_lines else 0)
    card_h = max(230, content_h + 76)
    card_y = 150
    card_bottom = card_y + card_h
    if card_bottom > height - 220:
        raise ValueError(
            "Opening title and hook are too long to fit safely in the opening card; "
            "shorten the hook/title instead of allowing clipped text."
        )

    # Shadow and translucent backing keep the words readable on bright footage.
    draw.rounded_rectangle(
        (card_x + 5, card_y + 9, card_x + card_w + 5, card_bottom + 9),
        radius=34, fill=(0, 0, 0, 95)
    )
    draw.rounded_rectangle(
        (card_x, card_y, card_x + card_w, card_bottom),
        radius=34, fill=(3, 12, 22, 208), outline=(105, 205, 201, 190), width=2
    )

    # Small accent rule, not part of the informational visual itself.
    draw.rounded_rectangle(
        (card_x + 42, card_y + 30, card_x + 126, card_y + 36),
        radius=3, fill=(89, 203, 190, 255)
    )

    y = card_y + 54
    if title_font:
        for line in title_lines:
            bbox = draw.textbbox((0, 0), line, font=title_font)
            x = (width - (bbox[2] - bbox[0])) // 2
            draw.text((x, y), line, font=title_font, fill=(156, 225, 216, 255), stroke_width=0)
            y += title_line_h
        y += 16

    # Center the complete hook block vertically in the remaining card area.
    hook_area_bottom = card_bottom - 30
    available_h = max(hook_block_h, hook_area_bottom - y)
    y += max(0, (available_h - hook_block_h) // 2)
    for line in hook_lines:
        bbox = draw.textbbox((0, 0), line, font=hook_font, stroke_width=1)
        line_w = bbox[2] - bbox[0]
        x = (width - line_w) // 2
        draw.text(
            (x, y), line, font=hook_font, fill=(255, 255, 255, 255),
            stroke_width=1, stroke_fill=(0, 0, 0, 130)
        )
        y += hook_line_h + 10

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return {
        "path": str(output),
        "title_lines": title_lines,
        "hook_lines": hook_lines,
        "title_text": title,
        "hook_text": hook,
        "title_font_size": getattr(title_font, "size", None) if title_font else None,
        "hook_font_size": getattr(hook_font, "size", None),
        "max_text_width": text_w,
        "card_bounds": [card_x, card_y, card_x + card_w, card_bottom],
    }
