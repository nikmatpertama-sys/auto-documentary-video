
"""
Local visual footage matcher using multilingual SigLIP.
Ranks candidate footage by comparing preview thumbnails with search queries.
Gracefully falls back to metadata ranking if model or thumbnails are unavailable.
"""

import hashlib
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

MODEL_NAME = "google/siglip-base-patch16-256-multilingual"
DEFAULT_CACHE = Path(".api-cache/visual-thumbnails")
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 120 * 1024 * 1024
BATCH_SIZE = 4
REQUEST_TIMEOUT = 15

_model = None
_processor = None
_torch = None
_Image = None
_load_attempted = False
_load_error = None


def _load_model():
    """Attempt model load only once per run; avoid repeating a bad download/import."""
    global _model, _processor, _torch, _Image, _load_attempted, _load_error

    if _model is not None:
        return True
    if _load_attempted:
        return False
    _load_attempted = True

    try:
        import torch
        from PIL import Image
        from transformers import AutoModel, AutoProcessor

        processor = AutoProcessor.from_pretrained(MODEL_NAME)
        model = AutoModel.from_pretrained(MODEL_NAME)
        model.eval()
        model.to("cpu")

        _torch = torch
        _Image = Image
        _processor = processor
        _model = model
        log.info("Loaded CPU visual matcher: %s", MODEL_NAME)
        return True

    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "SigLIP unavailable; one-time fallback to metadata ranking: %s",
            _load_error,
        )
        return False


def _prune_cache(cache_dir, max_bytes=MAX_CACHE_BYTES):
    """Keep the thumbnail cache bounded; evict least-recently-used files."""
    try:
        files = [p for p in Path(cache_dir).glob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= max_bytes:
            return

        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            if total <= max_bytes:
                break

    except OSError:
        log.debug("Thumbnail cache pruning skipped", exc_info=True)


def _thumbnail_url(candidate):
    """Accept common thumbnail field names from footage providers."""
    for key in ("thumbnail_url", "thumbnail", "preview_url", "image"):
        value = candidate.get(key)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return None


def _download_thumbnail(url, cache_dir):
    """Download and cache a thumbnail safely."""
    if not url:
        return None

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".img"

    filename = hashlib.sha256(url.encode("utf-8")).hexdigest() + suffix
    path = cache_dir / filename

    if path.exists():
        cached_size = path.stat().st_size
        if 0 < cached_size <= MAX_THUMBNAIL_BYTES:
            return path
        path.unlink(missing_ok=True)

    try:
        total = 0
        with requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            headers={"User-Agent": "AutoDocumentaryVideo/1.0"},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()

            if not content_type.startswith("image/"):
                return None

            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_THUMBNAIL_BYTES:
                return None

            with path.open("wb") as handle:
                for chunk in response.iter_content(65536):
                    if not chunk:
                        continue

                    total += len(chunk)
                    if total > MAX_THUMBNAIL_BYTES:
                        path.unlink(missing_ok=True)
                        return None

                    handle.write(chunk)

        if total == 0:
            path.unlink(missing_ok=True)
            return None

        return path

    except Exception as exc:
        log.debug("Thumbnail download failed: %s", exc)
        path.unlink(missing_ok=True)
        return None


def _metadata_score(candidate):
    """Read an existing metadata score without changing candidate data."""
    for key in ("metadata_score", "relevance_score", "score"):
        value = candidate.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _percentile_ranks(values):
    """Convert arbitrary scores into stable 0..1 rank scores."""
    if not values:
        return []

    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)

    if len(values) == 1:
        return [1.0]

    for rank, index in enumerate(order):
        result[index] = rank / (len(values) - 1)

    return result


def rank_candidates(
    candidates,
    query,
    cache_dir=DEFAULT_CACHE,
    visual_weight=0.78,
):
    """
    Rerank candidate dictionaries using thumbnail/text similarity.

    Each candidate may contain:
      - thumbnail_url (or thumbnail / preview_url / image)
      - query (optional candidate-specific visual query)
      - metadata_score, relevance_score, or score (optional)

    Returns a new sorted list. Candidates without thumbnails remain eligible.
    """
    items = [dict(item) for item in candidates or []]

    if len(items) < 2:
        return items

    metadata_values = [_metadata_score(item) for item in items]
    metadata_ranks = _percentile_ranks(metadata_values)

    thumbnail_urls = [_thumbnail_url(item) for item in items]

    if not any(thumbnail_urls) or not _load_model():
        for index, item in enumerate(items):
            item["visual_score"] = None
            item["visual_rank_score"] = None
            item["combined_rank_score"] = metadata_ranks[index]

        return sorted(
            items,
            key=lambda item: item["combined_rank_score"],
            reverse=True,
        )

    valid_indices = []
    images = []
    texts = []

    for index, item in enumerate(items):
        url = thumbnail_urls[index]
        path = _download_thumbnail(url, cache_dir)

        if path is None:
            continue

        try:
            with _Image.open(path) as opened:
                image = opened.convert("RGB")
                image.thumbnail((512, 512))

            images.append(image)
            valid_indices.append(index)

            selected_query = str(
                item.get("query") or query or ""
            ).strip()

            texts.append("Video footage showing " + selected_query)

        except Exception as exc:
            log.debug("Invalid preview image: %s", exc)

    visual_scores = [None] * len(items)

    if images:
        try:
            # Batch inference limits CPU memory use.
            for start in range(0, len(images), BATCH_SIZE):
                batch_images = images[start:start + BATCH_SIZE]
                batch_texts = texts[start:start + BATCH_SIZE]

                inputs = _processor(
                    text=batch_texts,
                    images=batch_images,
                    padding="max_length",
                    truncation=True,
                    return_tensors="pt",
                )

                with _torch.inference_mode():
                    outputs = _model(**inputs)

                # Paired image/text scores are on the diagonal.
                scores = (
                    outputs.logits_per_image
                    .diag()
                    .float()
                    .cpu()
                    .tolist()
                )

                for offset, score in enumerate(scores):
                    visual_scores[valid_indices[start + offset]] = float(score)

        except Exception as exc:
            log.warning(
                "SigLIP inference failed; using metadata ranking: %s",
                exc,
            )
            visual_scores = [None] * len(items)

        finally:
            for image in images:
                try:
                    image.close()
                except Exception:
                    pass

    _prune_cache(cache_dir)

    present = [
        index
        for index, score in enumerate(visual_scores)
        if score is not None
    ]

    visual_ranks = [None] * len(items)

    if present:
        ranked = _percentile_ranks(
            [visual_scores[i] for i in present]
        )

        for index, rank in zip(present, ranked):
            visual_ranks[index] = rank

    weight = min(1.0, max(0.0, float(visual_weight)))

    for index, item in enumerate(items):
        visual_rank = visual_ranks[index]

        item["visual_score"] = visual_scores[index]
        item["visual_rank_score"] = visual_rank

        if visual_rank is None:
            # No thumbnail: retain metadata-based eligibility.
            combined = metadata_ranks[index] * (1.0 - weight)
        else:
            combined = (
                weight * visual_rank
                + (1.0 - weight) * metadata_ranks[index]
            )

        item["combined_rank_score"] = combined

    return sorted(
        items,
        key=lambda item: item["combined_rank_score"],
        reverse=True,
    )
