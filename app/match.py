"""Assign reference stripe identities to freely-detected cells.

The model detects geometry; this module decides which detected cell is which
note. Keeping that decision in code means a count mismatch is an explicit,
inspectable discrepancy rather than a silent renumbering that transposes the
instrument.

Matching is order-preserving within a segment: detected cells are already in
left-to-right order, and reference stripes are ordered by stripeIndex, so the
assignment is a monotonic alignment between two sequences. When the counts
agree it is a straight zip. When they differ, a Needleman-Wunsch style alignment
picks which reference stripes went unmatched, and the run is flagged.
"""

from typing import Any

from app.reference import REFERENCE_CALIBRATION

REFERENCE_BY_SEGMENT: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}
for _stripe in REFERENCE_CALIBRATION["stripes"]:
    REFERENCE_BY_SEGMENT[_stripe["segment"]].append(_stripe)


def _centroid(polygon) -> tuple[float, float]:
    return (
        sum(p[0] for p in polygon) / len(polygon),
        sum(p[1] for p in polygon) / len(polygon),
    )


def _align(detected: list[dict], reference: list[dict], gap_penalty: float) -> list[tuple[int | None, int]]:
    """Monotonic alignment of detected cells to reference stripes.

    Returns [(detected_index | None, reference_index)] covering every reference
    stripe. Cost is the along-crosswalk distance between centroids after
    removing the median offset, so a uniformly drifted camera does not make
    every pair look expensive.
    """
    if not detected:
        return [(None, r) for r in range(len(reference))]

    detected_x = [_centroid(d["polygon"])[0] for d in detected]
    reference_x = [_centroid(r["polygon"])[0] for r in reference]

    # Remove a global shift so drift is not mistaken for mismatch.
    offset = sorted(detected_x)[len(detected_x) // 2] - sorted(reference_x)[len(reference_x) // 2]
    reference_shifted = [x + offset for x in reference_x]

    rows, cols = len(detected), len(reference)
    cost = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    back: list[list[str]] = [[""] * (cols + 1) for _ in range(rows + 1)]

    for i in range(1, rows + 1):
        cost[i][0] = i * gap_penalty
        back[i][0] = "d"
    for j in range(1, cols + 1):
        cost[0][j] = j * gap_penalty
        back[0][j] = "r"

    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            match = cost[i - 1][j - 1] + abs(detected_x[i - 1] - reference_shifted[j - 1])
            skip_detected = cost[i - 1][j] + gap_penalty
            skip_reference = cost[i][j - 1] + gap_penalty
            best = min(match, skip_detected, skip_reference)
            cost[i][j] = best
            back[i][j] = "m" if best == match else ("d" if best == skip_detected else "r")

    pairs: list[tuple[int | None, int]] = []
    i, j = rows, cols
    while i > 0 or j > 0:
        move = back[i][j]
        if move == "m":
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif move == "d":
            i -= 1  # a detected cell with no reference stripe: dropped
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return pairs


def match(detection: dict[str, Any], *, gap_penalty: float = 12.0) -> dict[str, Any]:
    """Turn a detection into a calibration keyed by reference stripeIndex."""
    cells = detection.get("cells", [])
    by_segment: dict[str, list[dict]] = {"left": [], "right": []}
    for cell in cells:
        segment = cell.get("segment")
        if segment in by_segment and (cell.get("polygon") or []):
            by_segment[segment].append(cell)
    for segment in by_segment:
        by_segment[segment].sort(key=lambda c: _centroid(c["polygon"])[0])

    stripes: list[dict[str, Any]] = []
    notes: list[str] = []
    counts: dict[str, Any] = {}

    for segment in ("left", "right"):
        reference = REFERENCE_BY_SEGMENT[segment]
        detected = by_segment[segment]
        counts[segment] = {"detected": len(detected), "expected": len(reference)}
        if len(detected) != len(reference):
            notes.append(
                f"{segment}: detected {len(detected)} cells, reference expects {len(reference)}"
            )

        for detected_index, reference_index in _align(detected, reference, gap_penalty):
            stripe = reference[reference_index]
            if detected_index is None:
                stripes.append(
                    {
                        "stripeIndex": stripe["stripeIndex"],
                        "segment": segment,
                        "note": stripe["note"],
                        "visible": False,
                        "polygon": [],
                        "reason": "no detected cell aligned to this reference stripe",
                    }
                )
                continue
            cell = detected[detected_index]
            stripes.append(
                {
                    "stripeIndex": stripe["stripeIndex"],
                    "segment": segment,
                    "note": stripe["note"],
                    "visible": True,
                    "polygon": cell["polygon"],
                    "occluded": bool(cell.get("occluded")),
                }
            )

    stripes.sort(key=lambda s: s["stripeIndex"])

    return {
        "leftCrosswalk": detection.get("leftCrosswalk"),
        "rightCrosswalk": detection.get("rightCrosswalk"),
        "stripes": stripes,
        "conditions": detection.get("conditions"),
        "confidence": detection.get("confidence"),
        "reasoning": detection.get("reasoning"),
        "matching": {"counts": counts, "notes": notes, "clean": not notes},
        "_usage": detection.get("_usage"),
        "_model": detection.get("_model"),
        "_frameSize": detection.get("_frameSize"),
    }
