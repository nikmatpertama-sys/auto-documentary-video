"""Retrieve published institutional reference visuals; never synthesize data figures."""
from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from reference_visuals import allowed_reuse_license, validate_reference_candidate

NASA_SEARCH = "https://images-api.nasa.gov/search"
NASA_ASSET = "https://images-api.nasa.gov/asset/{}"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "AutoDocumentaryVideoProject/4.0 (open educational documentary; source attribution enabled)"
MAX_IMAGE_BYTES = 35 * 1024 * 1024


def _clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def nasa_asset_reuse_eligible(metadata: str) -> bool:
    """Reject NASA library items explicitly marked as third-party/restricted."""
    value = re.sub(r"\s+", " ", str(metadata or "")).lower()
    restricted_markers = (
        "copyrighted material", "copyright protected", "used with permission",
        "all rights reserved", "permission required", "contact the copyright holder",
        "third-party copyright", "third party copyright",
    )
    return not any(marker in value for marker in restricted_markers)


def nasa_image_search(query: str, limit: int = 8) -> list[dict]:
    """Search NASA's official Image and Video Library for still images."""
    response = requests.get(
        NASA_SEARCH,
        params={"q": query, "media_type": "image", "page_size": min(12, max(1, limit))},
        headers={"User-Agent": USER_AGENT}, timeout=35,
    )
    response.raise_for_status()
    items = (response.json().get("collection") or {}).get("items") or []
    results = []
    for item in items[:limit]:
        data = (item.get("data") or [{}])[0]
        nasa_id = str(data.get("nasa_id") or "").strip()
        if not nasa_id:
            continue
        try:
            manifest_response = requests.get(
                NASA_ASSET.format(nasa_id), headers={"User-Agent": USER_AGENT}, timeout=35
            )
            manifest_response.raise_for_status()
            assets = (manifest_response.json().get("collection") or {}).get("items") or []
        except Exception:
            continue
        image_urls = []
        for asset in assets:
            href = str(asset.get("href") or "")
            clean = href.split("?", 1)[0].lower()
            if clean.endswith((".jpg", ".jpeg", ".png", ".webp")):
                image_urls.append(href)
        if not image_urls:
            continue
        # Prefer large/original versions while avoiding huge raw TIFF files.
        image_urls.sort(key=lambda u: (
            0 if "~large." in u.lower() or "~orig." in u.lower() else
            1 if "~medium." in u.lower() else 2,
            len(u),
        ))
        title = str(data.get("title") or "NASA image")
        description = str(data.get("description") or "")
        keywords = data.get("keywords") or []
        metadata = " ".join([title, description, " ".join(map(str, keywords))])
        rights_metadata = " ".join([
            metadata, str(data.get("photographer") or ""),
            str(data.get("secondary_creator") or ""), str(data.get("center") or ""),
        ])
        if not nasa_asset_reuse_eligible(rights_metadata):
            continue
        secondary = str(data.get("secondary_creator") or "")
        photographer = str(data.get("photographer") or "")
        center = str(data.get("center") or "NASA")
        results.append({
            "source": "NASA",
            "id": nasa_id,
            "title": title,
            "url": image_urls[0],
            "page_url": f"https://images.nasa.gov/details/{nasa_id}",
            "original_source_url": f"https://images.nasa.gov/details/{nasa_id}",
            "metadata": metadata,
            "publisher": f"NASA {center} {secondary}".strip(),
            "creator": photographer or secondary or center,
            "license": "NASA media usage guidelines; third-party credits must be followed where stated",
        })
    return results


def commons_image_search(query: str, limit: int = 12) -> list[dict]:
    """Search Commons images, preserving file-page, license and origin metadata."""
    response = requests.get(
        COMMONS_API,
        params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": 6,
            "gsrlimit": min(20, max(1, limit)), "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
        },
        headers={"User-Agent": USER_AGENT}, timeout=35,
    )
    response.raise_for_status()
    pages = (response.json().get("query") or {}).get("pages") or {}
    results = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime") or "").lower()
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            continue
        if int(info.get("width") or 0) < 900 or int(info.get("height") or 0) < 500:
            continue
        ext = info.get("extmetadata") or {}
        license_name = _clean_html((ext.get("LicenseShortName") or {}).get("value", ""))
        if not allowed_reuse_license(license_name):
            continue
        title = _clean_html(page.get("title", ""))
        description = _clean_html((ext.get("ImageDescription") or {}).get("value", ""))
        creator = _clean_html((ext.get("Artist") or {}).get("value", ""))
        source_text = _clean_html((ext.get("Source") or {}).get("value", ""))
        credit = _clean_html((ext.get("Credit") or {}).get("value", ""))
        publisher = " ".join(x for x in (source_text, credit) if x)
        original_source_url = ""
        for value in (ext.get("Source") or {}).get("value", ""), (ext.get("Credit") or {}).get("value", ""):
            match = re.search(r'href=["\'](https?://[^"\']+)', str(value), re.I)
            if match:
                original_source_url = html.unescape(match.group(1))
                break
        page_url = page.get("canonicalurl") or (
            "https://commons.wikimedia.org/wiki/" + str(page.get("title", "")).replace(" ", "_")
        )
        results.append({
            "source": "Wikimedia Commons",
            "id": str(page.get("pageid") or ""),
            "title": title,
            "url": str(info.get("url") or ""),
            "page_url": page_url,
            "original_source_url": original_source_url,
            "metadata": " ".join((title, description, source_text, credit)),
            "publisher": publisher,
            "creator": creator,
            "license": license_name,
            "license_url": _clean_html((ext.get("LicenseUrl") or {}).get("value", "")),
        })
    return results


def download_reference_image(url: str, destination: str | Path) -> tuple[int, int]:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(20, 90)) as response:
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"Reference asset did not return an image (Content-Type: {content_type}).")
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 512):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ValueError("Reference image exceeded the 35 MB safety limit.")
                output.write(chunk)
    if total < 10_000:
        destination.unlink(missing_ok=True)
        raise ValueError("Reference image is unexpectedly small.")
    try:
        with Image.open(destination) as image:
            image.verify()
        with Image.open(destination) as image:
            width, height = image.size
            if width < 700 or height < 400:
                raise ValueError(f"Reference visual resolution is too small ({width}x{height}).")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return width, height


def _font(size: int, bold: bool = True):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in paths:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _wrap_pixel(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        if not current or draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def compose_reference_frame(image_path: str | Path, output: str | Path,
                            title: str, source_label: str, source_url: str,
                            width: int = 1080, height: int = 1920) -> str:
    """Frame a published image without cropping or modifying its information."""
    source = Image.open(image_path).convert("RGB")
    canvas = Image.new("RGB", (width, height), (5, 13, 23))
    draw = ImageDraw.Draw(canvas)
    label_font = _font(24, bold=False)
    max_w = width - 96

    title = " ".join(str(title or "Published reference visual").split())
    title_font = _font(34)
    title_lines = _wrap_pixel(draw, title, title_font, max_w)
    while len(title_lines) > 3 and getattr(title_font, "size", 34) > 25:
        title_font = _font(getattr(title_font, "size", 34) - 2)
        title_lines = _wrap_pixel(draw, title, title_font, max_w)
    draw.text((48, 62), "PUBLISHED REFERENCE", font=label_font, fill=(121, 207, 198))
    y = 108
    title_line_height = max(34, title_font.getbbox("Ag")[3] - title_font.getbbox("Ag")[1])
    for line in title_lines:
        draw.text((48, y), line, font=title_font, fill=(246, 249, 252))
        y += title_line_height + 8

    area_top = max(230, y + 28)
    area_bottom = height - 330
    max_image = (width - 96, area_bottom - area_top)
    fitted = ImageOps.contain(source, max_image, method=Image.Resampling.LANCZOS)
    image_x = (width - fitted.width) // 2
    image_y = area_top + (max_image[1] - fitted.height) // 2
    pad = 18
    draw.rounded_rectangle(
        (image_x - pad, image_y - pad, image_x + fitted.width + pad, image_y + fitted.height + pad),
        radius=10, fill=(235, 239, 242),
    )
    canvas.paste(fitted, (image_x, image_y))

    footer_y = height - 275
    draw.line((48, footer_y - 28, width - 48, footer_y - 28), fill=(61, 91, 112), width=2)
    source_label = " ".join(str(source_label or "Institutional source").split())
    source_font = _font(24)
    source_lines = _wrap_pixel(draw, source_label, source_font, max_w)
    while len(source_lines) > 2 and getattr(source_font, "size", 24) > 18:
        source_font = _font(getattr(source_font, "size", 24) - 2)
        source_lines = _wrap_pixel(draw, source_label, source_font, max_w)
    source_line_h = max(28, source_font.getbbox("Ag")[3] - source_font.getbbox("Ag")[1])
    for index, line in enumerate(source_lines[:2]):
        draw.text((48, footer_y + index * (source_line_h + 3)), line,
                  font=source_font, fill=(244, 249, 252))
    host = (urlparse(source_url).hostname or "source link").replace("www.", "")
    host_font = _font(22, bold=False)
    while draw.textbbox((0, 0), host, font=host_font)[2] > max_w and getattr(host_font, "size", 22) > 16:
        host_font = _font(getattr(host_font, "size", 22) - 2, bold=False)
    draw.text((48, footer_y + 2 * (source_line_h + 3) + 4), host,
              font=host_font, fill=(153, 178, 196))

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=95)
    return str(output)


def choose_reference_candidate(candidates: list[dict], query: str) -> tuple[dict | None, str]:
    scored = []
    failures = []
    for candidate in candidates:
        ok, reason = validate_reference_candidate(candidate, query, min_match=0.45)
        if not ok:
            failures.append(f"{candidate.get('title', 'candidate')}: {reason}")
            continue
        metadata = " ".join([
            str(candidate.get("title", "")), str(candidate.get("metadata", "")),
            str(candidate.get("publisher", "")), str(candidate.get("creator", "")),
        ])
        from reference_visuals import query_match_ratio
        score = query_match_ratio(metadata, query)
        # NASA images are first-party assets; Commons assets are accepted only
        # when their metadata identifies an institutional origin.
        source_bonus = 0.08 if candidate.get("source") == "NASA" else 0.0
        scored.append((score + source_bonus, candidate))
    if not scored:
        return None, "No reusable institutional reference matched the requested subject. " + " | ".join(failures[:3])
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1], f"Selected institutional reference with metadata match {scored[0][0]:.2f}."
