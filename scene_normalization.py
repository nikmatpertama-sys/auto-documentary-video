"""Conservative repair for AI plans that return a near-miss scene count.

The video renderer expects eight scenes. This module only splits a two-sentence
scene when both sentences independently mention its visible focus, or merges
adjacent one-sentence scenes when their visible subjects and footage queries
are substantially the same. It never invents narration or silently drops a
scene. Unsafe cases are returned unchanged for the normal validator/provider
repair path to handle.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from sentence_utils import split_sentences


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", str(value or "").lower()))


def _normal(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", str(value or "").lower()))


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _sentence_records(scene: dict[str, Any], original_index: int) -> list[dict[str, Any]]:
    narration = re.sub(r"\s+", " ", str(scene.get("narration", ""))).strip()
    sentences = split_sentences(narration)
    if not 1 <= len(sentences) <= 2:
        return []
    return [
        {
            "scene": deepcopy(scene),
            "original_index": original_index,
            "sentence": sentence,
            "sentence_norm": _normal(sentence),
        }
        for sentence in sentences
    ]


def _can_split(group: dict[str, Any]) -> tuple[bool, float]:
    records = group["records"]
    if len(records) != 2:
        return False, 0.0
    scene = records[0]["scene"]
    focus = _tokens(scene.get("visual_focus"))
    if not 2 <= len(focus) <= 6:
        return False, 0.0
    scores = []
    for record in records:
        sentence_tokens = _tokens(record["sentence"])
        score = len(focus & sentence_tokens) / len(focus)
        if score < 0.5:
            return False, 0.0
        scores.append(score)
    # Both sentences independently name the same visible subject.
    return True, sum(scores) / len(scores)


def _can_merge(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, float]:
    if len(left["records"]) != 1 or len(right["records"]) != 1:
        return False, 0.0
    a = left["records"][0]["scene"]
    b = right["records"][0]["scene"]
    focus_a, focus_b = _tokens(a.get("visual_focus")), _tokens(b.get("visual_focus"))
    query_a, query_b = _tokens(a.get("visual_query")), _tokens(b.get("visual_query"))
    focus_score = _overlap(focus_a, focus_b)
    query_score = _overlap(query_a, query_b)
    if focus_score < 0.6 or query_score < 0.35:
        return False, 0.0

    mode_a = str(a.get("visual_mode") or "footage").strip().lower()
    mode_b = str(b.get("visual_mode") or "footage").strip().lower()
    if mode_a != mode_b:
        return False, 0.0
    if mode_a == "trusted_reference":
        if str(a.get("reference_query") or "").strip().lower() != str(b.get("reference_query") or "").strip().lower():
            return False, 0.0
        if str(a.get("source_url") or "").strip() != str(b.get("source_url") or "").strip():
            return False, 0.0

    return True, (focus_score * 0.7) + (query_score * 0.3)


def normalize_scene_count(candidate: Any, target_count: int = 8) -> Any:
    """Safely normalize a near-miss plan to the renderer's exact scene count.

    Returns the original candidate unchanged when there is no conservative
    lossless transformation. Fact-claim scene indices are remapped to the new
    scene groups; if any claim cannot be mapped unambiguously, no repair occurs.
    """
    if not isinstance(candidate, dict):
        return candidate
    source_scenes = candidate.get("scenes")
    if not isinstance(source_scenes, list) or len(source_scenes) == target_count:
        return candidate
    if not 1 <= len(source_scenes) <= 12:
        return candidate

    groups: list[dict[str, Any]] = []
    for original_index, scene in enumerate(source_scenes, 1):
        if not isinstance(scene, dict):
            return candidate
        records = _sentence_records(scene, original_index)
        if not records:
            return candidate
        groups.append({"records": records})

    # Too few scenes: split only scenes whose two sentences both independently
    # state the same visible focus. Never create filler narration.
    while len(groups) < target_count:
        choices = []
        for index, group in enumerate(groups):
            ok, score = _can_split(group)
            if ok:
                choices.append((score, index))
        if not choices:
            return candidate
        _score, index = max(choices)
        records = groups[index]["records"]
        groups[index:index + 1] = [
            {"records": [records[0]]},
            {"records": [records[1]]},
        ]

    # Too many scenes: merge only adjacent single-sentence scenes with the
    # same dominant visible subject and highly overlapping footage queries.
    while len(groups) > target_count:
        choices = []
        for index in range(len(groups) - 1):
            ok, score = _can_merge(groups[index], groups[index + 1])
            if ok:
                choices.append((score, index))
        if not choices:
            return candidate
        _score, index = max(choices)
        merged_records = groups[index]["records"] + groups[index + 1]["records"]
        groups[index:index + 2] = [{"records": merged_records}]

    # Build remapping from each original narration sentence to its new scene.
    sentence_to_new_scene: dict[tuple[int, str], int] = {}
    output_scenes: list[dict[str, Any]] = []
    for new_index, group in enumerate(groups, 1):
        records = group["records"]
        scene = deepcopy(records[0]["scene"])
        scene["narration"] = " ".join(record["sentence"] for record in records)

        if len(records) == 2 and records[0]["original_index"] != records[1]["original_index"]:
            # Preserve independently discovered alternative shots from both
            # source scenes, without changing the dominant primary query.
            alternatives: list[str] = []
            for record in records:
                source = record["scene"]
                for query in source.get("visual_queries", []) if isinstance(source.get("visual_queries"), list) else []:
                    query = re.sub(r"\s+", " ", str(query)).strip()
                    if query and query.lower() not in {item.lower() for item in alternatives}:
                        alternatives.append(query)
            if len(alternatives) >= 2:
                scene["visual_queries"] = alternatives[:2]

        for record in records:
            sentence_to_new_scene[(record["original_index"], record["sentence_norm"])] = new_index
        output_scenes.append(scene)

    # Remap every evidence-ledger entry; fail closed if the original scene or
    # exact sentence cannot be identified after the structural transformation.
    claims = candidate.get("fact_claims")
    if isinstance(claims, list):
        remapped_claims = deepcopy(claims)
        for claim in remapped_claims:
            if not isinstance(claim, dict):
                return candidate
            try:
                old_scene = int(claim.get("scene", 0))
            except (TypeError, ValueError):
                return candidate
            claim_norm = _normal(claim.get("sentence", ""))
            new_scene = sentence_to_new_scene.get((old_scene, claim_norm))
            if new_scene is None:
                # A claim may be a complete sentence copied from its source
                # narration with punctuation/case normalization only.
                matches = [
                    value for (source_index, source_sentence), value in sentence_to_new_scene.items()
                    if source_index == old_scene and claim_norm and claim_norm in source_sentence
                ]
                if len(set(matches)) != 1:
                    return candidate
                new_scene = matches[0]
            claim["scene"] = new_scene
    else:
        return candidate

    result = deepcopy(candidate)
    result["scenes"] = output_scenes
    result["fact_claims"] = remapped_claims
    return result
