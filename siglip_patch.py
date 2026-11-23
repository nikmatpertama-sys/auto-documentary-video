from pathlib import Path

MAIN_FILE = Path("main.py")

if not MAIN_FILE.exists():
    raise RuntimeError("main.py tidak ditemukan.")

source = MAIN_FILE.read_text(encoding="utf-8")

if "SIGLIP_VISUAL_MATCHER_PATCH_V2" in source:
    print("SigLIP patch V2 sudah diterapkan.")
    raise SystemExit(0)


def replace_once(source, old, new, label):
    count = source.count(old)

    if count != 1:
        raise RuntimeError(
            f"Gagal memasang {label}: "
            f"penanda ditemukan {count} kali, seharusnya 1. "
            "main.py tidak akan dijalankan."
        )

    return source.replace(old, new, 1)


# 1. Import matcher
source = replace_once(
    source,
    "import edge_tts\n",
    "import edge_tts\n\n"
    "# SIGLIP_VISUAL_MATCHER_PATCH_V2\n"
    "from visual_matcher import rank_candidates\n",
    "import SigLIP",
)


# 2. Thumbnail Pixabay
source = replace_once(
    source,
    '            "license": "Pixabay Content License",\n',
    '            "thumbnail_url": str(\n'
    '                rendition.get("thumbnail")\n'
    '                or hit.get("thumbnail")\n'
    '                or ""\n'
    '            ),\n'
    '            "license": "Pixabay Content License",\n',
    "thumbnail Pixabay",
)


# 3. Thumbnail Pexels
source = replace_once(
    source,
    '            "license": "Pexels License",\n',
    '            "thumbnail_url": str(video.get("image") or ""),\n'
    '            "license": "Pexels License",\n',
    "thumbnail Pexels",
)


# 4. Thumbnail NASA
source = replace_once(
    source,
    '            "license": (\n'
    '                "NASA media usage guidelines; "\n'
    '                "verify third-party credits where noted"\n'
    '            ),\n',
    '            "thumbnail_url": next(\n'
    '                (\n'
    '                    str(link.get("href") or "")\n'
    '                    for link in (item.get("links") or [])\n'
    '                    if link.get("rel") == "preview"\n'
    '                ),\n'
    '                ""\n'
    '            ),\n'
    '            "license": (\n'
    '                "NASA media usage guidelines; "\n'
    '                "verify third-party credits where noted"\n'
    '            ),\n',
    "thumbnail NASA",
)


# 5. Thumbnail Wikimedia Commons
source = replace_once(
    source,
    '            "iiprop": "url|size|mime|extmetadata"\n',
    '            "iiprop": "url|size|mime|extmetadata",\n'
    '            "iiurlwidth": 640\n',
    "thumbnail Commons API",
)

source = replace_once(
    source,
    '            "license": license_name or "See file page",\n',
    '            "thumbnail_url": str(info.get("thumburl") or ""),\n'
    '            "license": license_name or "See file page",\n',
    "thumbnail Commons",
)


# 6. Invalidasi cache hasil pencarian lama
source = replace_once(
    source,
    '            (\n'
    '                cache_sources + "|" + query.lower()\n'
    '            ).encode()\n',
    '            (\n'
    '                "siglip-thumbnails-v1|"\n'
    '                + cache_sources + "|" + query.lower()\n'
    '            ).encode()\n',
    "cache versi thumbnail",
)


# 7. Integrasi SigLIP pada pemeringkatan kandidat
source = replace_once(
    source,
    '        ranked = sorted(\n'
    '            candidates.values(),\n'
    '            key=lambda item: item[0],\n'
    '            reverse=True\n'
    '        )\n',
    '        ranked = sorted(\n'
    '            candidates.values(),\n'
    '            key=lambda item: item[0],\n'
    '            reverse=True\n'
    '        )\n'
    '\n'
    '        if len(ranked) > 1:\n'
    '            visual_pool_size = min(\n'
    '                len(ranked),\n'
    '                max(VISION_CANDIDATE_LIMIT * 2, 12)\n'
    '            )\n'
    '\n'
    '            visual_pool = ranked[:visual_pool_size]\n'
    '            visual_inputs = []\n'
    '\n'
    '            for candidate_item in visual_pool:\n'
    '                score, hit, candidate_query, matched, total = candidate_item\n'
    '\n'
    '                visual_inputs.append({\n'
    '                    **hit,\n'
    '                    "metadata_score": score,\n'
    '                    "query": candidate_query,\n'
    '                    "_original_item": candidate_item,\n'
    '                })\n'
    '\n'
    '            try:\n'
    '                visual_order = rank_candidates(\n'
    '                    visual_inputs,\n'
    '                    query=(\n'
    '                        scene.get("visual_query")\n'
    '                        or scene.get("narration", "")\n'
    '                    ),\n'
    '                    cache_dir=CACHE / "visual-thumbnails",\n'
    '                    visual_weight=0.78,\n'
    '                )\n'
    '\n'
    '                ranked = [\n'
    '                    item["_original_item"]\n'
    '                    for item in visual_order\n'
    '                ] + ranked[visual_pool_size:]\n'
    '\n'
    '                print(\n'
    '                    f"Scene {i}: SigLIP reranked "\n'
    '                    f"{visual_pool_size} footage candidates."\n'
    '                )\n'
    '\n'
    '            except Exception as exc:\n'
    '                print(\n'
    '                    f"Scene {i}: SigLIP unavailable; "\n'
    '                    f"keeping metadata ranking: {exc}"\n'
    '                )\n',
    "pemeringkatan SigLIP",
)


# 8. Tandai parameter retry sementara pada visual verifier
source = replace_once(
    source,
    'def verify_footage_visually(\n'
    '    video_path,\n'
    '    scene,\n'
    '    query,\n'
    '    candidate_metadata=""\n'
    '):',
    'def verify_footage_visually(\n'
    '    video_path,\n'
    '    scene,\n'
    '    query,\n'
    '    candidate_metadata="",\n'
    '    _cooldown_retry=False\n'
    '):',
    "parameter retry visual verifier",
)


# 9. Matikan semua model OpenRouter jika kuota gratis harian habis
source = replace_once(
    source,
    '            if exc.status_code == 429:\n'
    '                delay = _provider_retry_delay(\n'
    '                    str(exc)\n'
    '                )',
    '            if exc.status_code == 429:\n'
    '                error_text = str(exc).lower()\n'
    '                daily_quota_exhausted = (\n'
    '                    "free-models-per-day" in error_text\n'
    '                    or "free_daily_limit" in error_text\n'
    '                    or (\n'
    '                        "x-ratelimit-remaining" in error_text\n'
    '                        and bool(re.search(\n'
    '                            r"x-ratelimit-remaining.{0,40}0",\n'
    '                            error_text,\n'
    '                        ))\n'
    '                    )\n'
    '                )\n'
    '\n'
    '                if (\n'
    '                    provider_id.startswith("OpenRouter::")\n'
    '                    and daily_quota_exhausted\n'
    '                ):\n'
    '                    for configured_id, _, _ in providers:\n'
    '                        if configured_id.startswith("OpenRouter::"):\n'
    '                            VISION_DISABLED_PROVIDERS.add(configured_id)\n'
    '\n'
    '                    print(\n'
    '                        "Visual verifier: OpenRouter free daily quota "\n'
    '                        "exhausted; disabling all OpenRouter vision "\n'
    '                        "routes for this workflow run."\n'
    '                    )\n'
    '                    continue\n'
    '\n'
    '                delay = _provider_retry_delay(\n'
    '                    str(exc)\n'
    '                )',
    "deteksi kuota harian OpenRouter",
)


# 10. Coba ulang sekali jika provider pulih setelah cooldown
source = replace_once(
    source,
    '    if not providers:\n'
    '        detail = (\n'
    '            "No visual AI credentials are configured."\n'
    '        )',
    '    if not _cooldown_retry:\n'
    '        recovered_route = None\n'
    '\n'
    '        for provider_id, provider_name, _ in providers:\n'
    '            if provider_id in VISION_DISABLED_PROVIDERS:\n'
    '                continue\n'
    '\n'
    '            if _provider_cooldown_remaining(provider_id) <= 0:\n'
    '                recovered_route = provider_name\n'
    '                break\n'
    '\n'
    '        if recovered_route:\n'
    '            print(\n'
    '                f"Visual verifier: {recovered_route} is available again; "\n'
    '                "retrying this footage candidate once."\n'
    '            )\n'
    '            return verify_footage_visually(\n'
    '                video_path,\n'
    '                scene,\n'
    '                query,\n'
    '                candidate_metadata,\n'
    '                _cooldown_retry=True,\n'
    '            )\n'
    '\n'
    '        cooldowns = [\n'
    '            _provider_cooldown_remaining(provider_id)\n'
    '            for provider_id, _, _ in providers\n'
    '            if provider_id not in VISION_DISABLED_PROVIDERS\n'
    '            and 0 < _provider_cooldown_remaining(provider_id) <= 60\n'
    '        ]\n'
    '\n'
    '        if cooldowns:\n'
    '            wait_seconds = max(1, int(max(cooldowns)) + 1)\n'
    '            print(\n'
    '                "Visual verifier: waiting "\n'
    '                f"{wait_seconds}s for provider cooldowns, then retrying "\n'
    '                "this footage candidate once."\n'
    '            )\n'
    '            time.sleep(wait_seconds)\n'
    '            return verify_footage_visually(\n'
    '                video_path,\n'
    '                scene,\n'
    '                query,\n'
    '                candidate_metadata,\n'
    '                _cooldown_retry=True,\n'
    '            )\n'
    '\n'
    '    if not providers:\n'
    '        detail = (\n'
    '            "No visual AI credentials are configured."\n'
    '        )',
    "retry satu kali setelah cooldown",
)


MAIN_FILE.write_text(source, encoding="utf-8")

print("SigLIP V2 dan visual verifier cooldown recovery berhasil diterapkan ke main.py.")
