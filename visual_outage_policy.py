"""Conservative metadata-only acceptance policy for temporary vision outages."""
from __future__ import annotations


def metadata_fallback_decision(
    query_score: float,
    query_matched: int,
    query_total: int,
    narration_score: float,
    narration_matched: int,
    narration_total: int,
) -> tuple[bool, str]:
    """Allow only strongly relevant footage when every visual AI is unavailable.

    The direct-query route preserves the existing strict threshold. The
    narration route handles over-specific/camera-oriented search phrases (for
    example, "NASA Landsat satellite image") when the actual narration still
    strongly matches the candidate's subject and setting.
    """
    q_coverage = query_matched / max(1, query_total)
    n_coverage = narration_matched / max(1, narration_total)

    if query_score >= 0.52 and q_coverage >= 0.50:
        return True, (
            f"strong query metadata match ({query_score:.2f}; "
            f"{query_matched}/{query_total})"
        )

    if (
        narration_score >= 0.50
        and narration_matched >= 2
        and n_coverage >= 0.30
    ):
        return True, (
            f"strong narration-context metadata match ({narration_score:.2f}; "
            f"{narration_matched}/{narration_total})"
        )

    return False, (
        f"insufficient metadata relevance: query {query_score:.2f} "
        f"({query_matched}/{query_total}), narration {narration_score:.2f} "
        f"({narration_matched}/{narration_total})"
    )
