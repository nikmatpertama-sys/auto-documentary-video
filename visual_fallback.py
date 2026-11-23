"""API-free procedural visual fallback for scenes without usable footage.

Creates an original animated 2D documentary explainer card using Pillow + FFmpeg.
It is intentionally an illustrated/motion-graphics fallback, not photorealistic 3D.
"""
from __future__ import annotations

import hashlib
import math
import re
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1080, 1920


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for item in candidates:
        if Path(item).exists():
            return ImageFont.truetype(item, size)
    return ImageFont.load_default()


def _wrap(draw, text, font, width):
    words = re.sub(r"\s+", " ", str(text or "")).split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def create_fallback_video(scene: dict, scene_number: int, output: str | Path,
                          duration: float, topic: str = "") -> Path:
    """Render a scene-specific illustrated card to a vertical H.264 MP4."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    seed = hashlib.sha256(
        (topic + str(scene.get("visual_query", "")) + str(scene.get("narration", ""))).encode()
    ).digest()
    hue = int(seed[0])
    palettes = [
        ((8, 18, 34), (15, 65, 82), (53, 205, 190)),
        ((20, 14, 35), (69, 38, 91), (206, 139, 255)),
        ((13, 24, 20), (31, 78, 55), (156, 220, 116)),
        ((28, 19, 12), (100, 55, 29), (255, 185, 91)),
        ((12, 18, 39), (36, 52, 104), (125, 173, 255)),
    ]
    bg, mid, accent = palettes[hue % len(palettes)]
    image = Image.new("RGB", (W, H), bg)
    px = image.load()
    for y in range(H):
        t = y / max(1, H - 1)
        col = tuple(int(bg[c] * (1-t) + mid[c] * t) for c in range(3))
        for x in range(W):
            px[x, y] = col

    # Soft glowing orbital/diagram shapes make each generated scene distinct.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx = 540 + (seed[1] - 128) * 0.55
    cy = 700 + (seed[2] - 128) * 0.7
    radius = 280 + seed[3]
    gd.ellipse((cx-radius, cy-radius, cx+radius, cy+radius),
               outline=(*accent, 145), width=8)
    gd.ellipse((cx-radius*.68, cy-radius*.68, cx+radius*.68, cy+radius*.68),
               outline=(*accent, 90), width=4)
    for k in range(7):
        angle = (seed[(k+4) % len(seed)] / 255) * math.tau
        x = cx + math.cos(angle) * radius * .82
        y = cy + math.sin(angle) * radius * .82
        r = 18 + (seed[(k+12) % len(seed)] % 28)
        gd.ellipse((x-r, y-r, x+r, y+r), fill=(*accent, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(3))
    image = Image.alpha_composite(image.convert("RGBA"), glow)
    draw = ImageDraw.Draw(image)

    eyebrow = f"FIELD NOTES  /  SCENE {scene_number:02d}"
    draw.text((76, 145), eyebrow, font=_font(28, True), fill=(*accent, 255))
    draw.rounded_rectangle((76, 205, 1004, 211), radius=3, fill=(*accent, 180))

    title = str(scene.get("visual_query") or scene.get("title") or topic or "DOCUMENTARY EXPLAINER")
    title = re.sub(r"[\r\n]+", " ", title).strip()[:110]
    title_font = _font(66, True)
    title_lines = _wrap(draw, title.upper(), title_font, 900)[:3]
    y = 300
    for line in title_lines:
        draw.text((76, y), line, font=title_font, fill=(255, 255, 255, 255),
                  stroke_width=1, stroke_fill=(0, 0, 0, 100))
        y += 86

    # Central abstract subject panel with a technical grid.
    panel_top, panel_bottom = 760, 1235
    draw.rounded_rectangle((76, panel_top, 1004, panel_bottom),
                           radius=34, fill=(4, 12, 24, 115),
                           outline=(*accent, 180), width=3)
    for gx in range(110, 1000, 90):
        draw.line((gx, panel_top+25, gx, panel_bottom-25), fill=(255,255,255,18), width=1)
    for gy in range(panel_top+25, panel_bottom, 70):
        draw.line((100, gy, 980, gy), fill=(255,255,255,18), width=1)
    # Stylized concentric visual, intentionally generic rather than false literal footage.
    center=(540, 990)
    for r, alpha, width in [(190,90,5),(135,120,4),(78,180,5)]:
        draw.ellipse((center[0]-r,center[1]-r,center[0]+r,center[1]+r),
                     outline=(*accent,alpha),width=width)
    draw.ellipse((center[0]-28,center[1]-28,center[0]+28,center[1]+28),
                 fill=(*accent,235))
    for k in range(5):
        x=180+((seed[k+20]*3)%720)
        yy=panel_top+55+((seed[k+27]*2)%(panel_bottom-panel_top-110))
        draw.ellipse((x-8,yy-8,x+8,yy+8),fill=(*accent,210))

    narration = str(scene.get("narration") or "")
    body_font = _font(34)
    body_lines = _wrap(draw, narration, body_font, 900)[:5]
    body_y = 1320
    for line in body_lines:
        draw.text((78, body_y), line, font=body_font, fill=(235, 242, 249, 255))
        body_y += 53

    draw.text((78, 1695), "ILLUSTRATED VISUAL  •  NOT ARCHIVAL FOOTAGE",
              font=_font(22, True), fill=(*accent, 255))
    draw.text((78, 1760), "AUTO-GENERATED DOCUMENTARY GRAPHIC",
              font=_font(20), fill=(195, 210, 225, 220))
    png = output.with_suffix(".png")
    image.convert("RGB").save(png, quality=94)

    safe_duration = max(1.0, float(duration))
    frames = max(30, int(math.ceil(safe_duration * 30)))
    # Gentle Ken Burns motion; still image remains readable.
    vf = (
        f"scale=1200:2134,zoompan=z='min(1.10,1+0.00035*on)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={W}x{H}:fps=30,format=yuv420p"
    )
    command = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(png),
        "-t", f"{safe_duration:.3f}", "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "21",
        "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not output.exists() or output.stat().st_size < 1000:
        raise RuntimeError("Local visual fallback render failed: " + result.stderr[-1200:])
    return output
