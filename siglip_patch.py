from pathlib import Path

MAIN_FILE = Path("main.py")

if not MAIN_FILE.exists():
    raise RuntimeError("main.py tidak ditemukan.")

source = MAIN_FILE.read_text(encoding="utf-8")

if "SIGLIP_VISUAL_MATCHER_PATCH_V1" in source:
    print("SigLIP patch sudah diterapkan.")
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
    "# SIGLIP_VISUAL_MATCHER_PATCH_V1\n"
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


MAIN_FILE.write_text(source, encoding="utf-8")

print("SigLIP integration berhasil diterapkan ke main.py.")
