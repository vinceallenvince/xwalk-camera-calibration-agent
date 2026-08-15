"""Run-over-run continuity for crosswalk boundaries.

detect_boundaries names crosswalks by their left-to-right order among the
boundaries it found *this run*. That is the right camera-agnostic default for
a first sighting, but it re-derives segment identity from scratch every run —
and the web client maps segment names to fixed pitch anchors, so a name that
drifts between runs transposes a whole keyboard. Miss the left crosswalk for
one frame (truck, glare) and the right crosswalk becomes position 0, gets
named "left", and plays from the wrong anchor until the next good run.

This module supplies the temporal memory: detected boundaries are matched to
the previously *published* calibration by spatial overlap, matched boundaries
adopt their previous segment names, and the disappearance of a previously
known crosswalk is reported as a regression so the caller can hold the last
good publish instead of overwriting it with a renamed or merged one.

The camera is fixed, so matching does not need precise polygon intersection:
axis-aligned bounding-box IoU is stable under the detector's frame-to-frame
outline wobble and cheap enough to run every cycle.
"""

from typing import Any

Polygon = list[list[float]]

# Below this overlap a detected boundary is not the same crosswalk as the
# previous one. Generous on purpose: consecutive runs of a fixed camera
# overlap almost entirely, and a genuinely re-aimed camera overlaps ~0.
MIN_MATCH_IOU = 0.2

# Positional naming order, mirrored from tools.SEGMENT_NAMES (not imported to
# keep this module dependency-free for tests).
_POSITIONAL_NAMES = ("left", "right")


def _bbox(polygon: Polygon) -> tuple[float, float, float, float]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_iou(a: Polygon, b: Polygon) -> float:
    """Intersection-over-union of the polygons' axis-aligned bounding boxes."""
    if len(a) < 3 or len(b) < 3:
        return 0.0

    a_left, a_top, a_right, a_bottom = _bbox(a)
    b_left, b_top, b_right, b_bottom = _bbox(b)

    overlap_w = min(a_right, b_right) - max(a_left, b_left)
    overlap_h = min(a_bottom, b_bottom) - max(a_top, b_top)
    if overlap_w <= 0 or overlap_h <= 0:
        return 0.0

    intersection = overlap_w * overlap_h
    union = (
        (a_right - a_left) * (a_bottom - a_top)
        + (b_right - b_left) * (b_bottom - b_top)
        - intersection
    )
    return intersection / union if union > 0 else 0.0


def _positional_name(position: int) -> str:
    if position < len(_POSITIONAL_NAMES):
        return _POSITIONAL_NAMES[position]
    return f"segment{position + 1}"


def _next_free_name(taken: set[str]) -> str:
    position = 0
    while _positional_name(position) in taken:
        position += 1
    return _positional_name(position)


def reconcile_segments(
    detected: dict[str, Polygon],
    previous: dict[str, Polygon] | None,
) -> tuple[dict[str, Polygon], dict[str, Any]]:
    """Carry segment names forward from the previous published calibration.

    Returns the detected boundaries re-keyed so that a boundary overlapping a
    previous one keeps the previous segment name, plus a continuity report:

      regression  — a previously published crosswalk was not found this run
                    (including the everything-failed case where `detected` is
                    empty). The caller should hold the previous publish.
      missing     — the previous segment names that went unmatched.
      renamed     — positional name → adopted previous name, for the log.

    With no previous calibration the detected names pass through untouched —
    a first sighting has nothing to stay consistent with.
    """
    if not previous:
        return dict(detected), {"regression": False, "missing": [], "renamed": {}}

    usable_previous = {name: poly for name, poly in previous.items() if len(poly) >= 3}

    # Score every detected/previous pairing, best overlap first, one-to-one.
    scored = sorted(
        (
            (bbox_iou(polygon, previous_polygon), detected_name, previous_name)
            for detected_name, polygon in detected.items()
            for previous_name, previous_polygon in usable_previous.items()
        ),
        key=lambda entry: -entry[0],
    )

    adopted: dict[str, str] = {}
    matched_previous: set[str] = set()
    for iou, detected_name, previous_name in scored:
        if iou < MIN_MATCH_IOU:
            break
        if detected_name in adopted or previous_name in matched_previous:
            continue
        adopted[detected_name] = previous_name
        matched_previous.add(previous_name)

    # Matched boundaries take their previous names; unmatched ones keep their
    # positional name unless an adopted name now claims it.
    reconciled: dict[str, Polygon] = {}
    taken = set(adopted.values())
    for detected_name, polygon in detected.items():
        if detected_name in adopted:
            reconciled[adopted[detected_name]] = polygon
        elif detected_name not in taken:
            reconciled[detected_name] = polygon
            taken.add(detected_name)
        else:
            free = _next_free_name(taken)
            reconciled[free] = polygon
            taken.add(free)

    missing = sorted(set(usable_previous) - matched_previous)
    renamed = {d: p for d, p in adopted.items() if d != p}

    return reconciled, {
        "regression": len(missing) > 0,
        "missing": missing,
        "renamed": renamed,
    }
