"""Structured-output schema for the calibration model, and the gates a run must
clear before its geometry may be trusted.

The schema is handed to Gemini as a `responseSchema` so the model is constrained
at decode time; the gates then re-check everything that a schema cannot express.
"""

from typing import Any

from app.reference import REFERENCE_CALIBRATION, SEGMENT_BY_INDEX, STRIPE_COUNT

REASONING_MAX_CHARS = 250

OBSTRUCTIONS = ["none", "snow", "vehicle", "construction", "glare", "darkness", "other"]
CAMERA_MOVED = ["none", "slight", "significant"]

# Vertex structured output. Note there is no "polygon: null" here — OpenAPI
# schema nullability is awkward across clients, so an unseen stripe returns
# visible=false with an empty polygon, and the gates treat that as "not seen".
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["leftCrosswalk", "rightCrosswalk", "stripes", "conditions", "confidence", "reasoning"],
    "properties": {
        "leftCrosswalk": {
            "type": "ARRAY",
            "description": "Quadrilateral for the left crosswalk, 4 [x,y] points, or empty if not visible.",
            "items": {"type": "ARRAY", "items": {"type": "NUMBER"}},
        },
        "rightCrosswalk": {
            "type": "ARRAY",
            "description": "Quadrilateral for the right crosswalk, 4 [x,y] points, or empty if not visible.",
            "items": {"type": "ARRAY", "items": {"type": "NUMBER"}},
        },
        "stripes": {
            "type": "ARRAY",
            "description": f"Exactly {STRIPE_COUNT} entries, one per reference stripeIndex, in ascending index order.",
            "items": {
                "type": "OBJECT",
                "required": ["stripeIndex", "visible", "polygon"],
                "properties": {
                    "stripeIndex": {"type": "INTEGER"},
                    "visible": {"type": "BOOLEAN"},
                    "polygon": {
                        "type": "ARRAY",
                        "description": "4 [x,y] points, or empty when visible is false.",
                        "items": {"type": "ARRAY", "items": {"type": "NUMBER"}},
                    },
                    "reason": {"type": "STRING", "description": "Why this stripe is not visible."},
                },
            },
        },
        "conditions": {
            "type": "OBJECT",
            "required": ["crosswalkVisible", "obstruction", "cameraMoved", "repaintSuspected"],
            "properties": {
                "crosswalkVisible": {"type": "BOOLEAN"},
                "obstruction": {"type": "STRING", "enum": OBSTRUCTIONS},
                "cameraMoved": {"type": "STRING", "enum": CAMERA_MOVED},
                "repaintSuspected": {"type": "BOOLEAN"},
            },
        },
        "confidence": {"type": "NUMBER", "description": "0.0 to 1.0."},
        "reasoning": {
            "type": "STRING",
            "description": f"Why this geometry and this status. Maximum {REASONING_MAX_CHARS} characters.",
        },
    },
}


def _polygon_area(polygon: list[list[float]]) -> float:
    """Shoelace area; sign-independent."""
    total = 0.0
    for index, (x, y) in enumerate(polygon):
        next_x, next_y = polygon[(index + 1) % len(polygon)]
        total += x * next_y - next_x * y
    return abs(total) / 2


def _centroid(polygon: list[list[float]]) -> tuple[float, float]:
    return (
        sum(p[0] for p in polygon) / len(polygon),
        sum(p[1] for p in polygon) / len(polygon),
    )


REFERENCE_STRIPE_BY_INDEX = {s["stripeIndex"]: s for s in REFERENCE_CALIBRATION["stripes"]}


def validate(payload: dict[str, Any], *, area_tolerance: float = 0.6, drift_cap_px: float = 60.0) -> dict[str, Any]:
    """Re-check what the schema cannot express.

    Returns {"passed": bool, "failures": [...], "warnings": [...], "metrics": {...}}.
    A failure means the geometry must not be published; a warning is recorded but
    does not block. The frame is 352 x 240, but note the reference itself has
    right-segment stripes extending past x=352, so bounds are checked with slack
    rather than hard-clipped.
    """
    failures: list[str] = []
    warnings: list[str] = []

    stripes = payload.get("stripes", [])

    # --- the musical contract -------------------------------------------------
    if len(stripes) != STRIPE_COUNT:
        failures.append(f"expected {STRIPE_COUNT} stripes, got {len(stripes)}")

    indexes = [s.get("stripeIndex") for s in stripes]
    if indexes != sorted(indexes):
        failures.append("stripeIndex values are not in ascending order")
    if len(set(indexes)) != len(indexes):
        failures.append("duplicate stripeIndex values")
    missing = set(REFERENCE_STRIPE_BY_INDEX) - set(indexes)
    if missing:
        failures.append(f"missing stripeIndex values: {sorted(missing)}")
    unknown = set(indexes) - set(REFERENCE_STRIPE_BY_INDEX)
    if unknown:
        failures.append(f"unknown stripeIndex values: {sorted(unknown)}")

    visible = [s for s in stripes if s.get("visible") and s.get("polygon")]
    if len(visible) < STRIPE_COUNT * 0.5:
        failures.append(f"only {len(visible)}/{STRIPE_COUNT} stripes visible; too few to trust")

    # --- geometry -------------------------------------------------------------
    displacements: list[float] = []
    for stripe in visible:
        index = stripe.get("stripeIndex")
        polygon = stripe.get("polygon") or []
        label = f"stripe {index}"

        if len(polygon) != 4 or any(len(p) != 2 for p in polygon):
            failures.append(f"{label}: expected 4 [x,y] points, got {len(polygon)}")
            continue
        if any(not all(isinstance(v, (int, float)) for v in p) for p in polygon):
            failures.append(f"{label}: non-numeric coordinate")
            continue

        area = _polygon_area(polygon)
        if area <= 0.5:
            failures.append(f"{label}: degenerate polygon (area {area:.2f})")
            continue

        reference = REFERENCE_STRIPE_BY_INDEX.get(index)
        if not reference:
            continue
        reference_area = _polygon_area([list(p) for p in reference["polygon"]])
        if reference_area > 0:
            ratio = area / reference_area
            if not (1 - area_tolerance) <= ratio <= (1 + area_tolerance) * 2:
                warnings.append(f"{label}: area ratio {ratio:.2f} vs reference")

        rx, ry = _centroid([list(p) for p in reference["polygon"]])
        cx, cy = _centroid(polygon)
        displacement = ((cx - rx) ** 2 + (cy - ry) ** 2) ** 0.5
        displacements.append(displacement)

        # Segment must match the reference; a stripe cannot change sides.
        if stripe.get("segment") and stripe["segment"] != SEGMENT_BY_INDEX.get(index):
            failures.append(f"{label}: segment changed from reference")

    max_displacement = max(displacements) if displacements else 0.0
    mean_displacement = (sum(displacements) / len(displacements)) if displacements else 0.0
    if max_displacement > drift_cap_px:
        failures.append(
            f"max stripe displacement {max_displacement:.1f}px exceeds the {drift_cap_px}px re-aim cap; needs review"
        )

    # --- ordering along the crosswalk ----------------------------------------
    for segment in ("left", "right"):
        centroids = [
            (s["stripeIndex"], _centroid(s["polygon"]))
            for s in visible
            if SEGMENT_BY_INDEX.get(s.get("stripeIndex")) == segment and len(s.get("polygon") or []) == 4
        ]
        xs = [c[1][0] for c in centroids]
        if len(xs) > 2 and xs != sorted(xs):
            failures.append(f"{segment} segment: stripe centroids are not monotonic left-to-right")

    # --- seam contiguity ------------------------------------------------------
    # Cells are meant to tile: cell N's right edge is cell N+1's left edge, with
    # the seam running down the middle of the gap between painted bars. Measured
    # as a warning while the prompt is being tuned; promote to a failure once
    # the model holds the contract reliably.
    by_index = {s.get("stripeIndex"): s for s in visible}
    seam_gaps: list[float] = []
    for index in sorted(by_index):
        nxt = by_index.get(index + 1)
        current = by_index[index]
        if not nxt or SEGMENT_BY_INDEX.get(index) != SEGMENT_BY_INDEX.get(index + 1):
            continue
        a, b = current.get("polygon") or [], nxt.get("polygon") or []
        if len(a) != 4 or len(b) != 4:
            continue
        # polygon order is [top-left, top-right, bottom-right, bottom-left]
        top_gap = ((a[1][0] - b[0][0]) ** 2 + (a[1][1] - b[0][1]) ** 2) ** 0.5
        bottom_gap = ((a[2][0] - b[3][0]) ** 2 + (a[2][1] - b[3][1]) ** 2) ** 0.5
        seam_gaps.append(max(top_gap, bottom_gap))

    max_seam_gap = max(seam_gaps) if seam_gaps else 0.0
    if max_seam_gap > 2.0:
        warnings.append(
            f"cells are not contiguous: worst seam mismatch {max_seam_gap:.1f}px between adjacent cells"
        )

    # --- crosswalk quads ------------------------------------------------------
    for key in ("leftCrosswalk", "rightCrosswalk"):
        polygon = payload.get(key) or []
        if not polygon:
            warnings.append(f"{key}: not returned")
            continue
        if len(polygon) != 4:
            failures.append(f"{key}: expected 4 points, got {len(polygon)}")
        elif _polygon_area(polygon) <= 1:
            failures.append(f"{key}: degenerate polygon")

    return {
        "passed": not failures,
        "failures": failures,
        "warnings": warnings,
        "metrics": {
            "visibleStripes": len(visible),
            "totalStripes": len(stripes),
            "maxDisplacementPx": round(max_displacement, 2),
            "meanDisplacementPx": round(mean_displacement, 2),
            "maxSeamGapPx": round(max_seam_gap, 2),
        },
    }


def derive_status(payload: dict[str, Any], validation: dict[str, Any]) -> str:
    """Map conditions + gate results onto the status the web client renders."""
    conditions = payload.get("conditions", {})
    if not conditions.get("crosswalkVisible", False):
        return "no_crosswalk"
    if any("re-aim cap" in f for f in validation["failures"]):
        return "needs_review"
    if not validation["passed"]:
        return "degraded"
    if conditions.get("cameraMoved") == "significant":
        return "needs_review"
    if payload.get("confidence", 0) < 0.6 or conditions.get("obstruction") not in ("none", None):
        return "degraded"
    return "ok"
