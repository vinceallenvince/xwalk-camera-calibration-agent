"""Camera-agnostic geometry for turning raw detections into stripe positions.

Nothing here knows about View 5056, musical notes, or how many stripes a
crosswalk "should" have. Given a crosswalk boundary polygon and a set of
detected stripe polygons, it answers one question: where does each stripe sit
along the crosswalk?

The answer is an integer slot index measured from the crosswalk's leading edge
in units of the stripe pitch. Two properties make it usable as a stable
identity:

  - It is derived from the boundary, not from the detections, so a stripe
    keeps its index when a car occludes its neighbours.
  - The pitch is the *median* gap between detections, so it survives missing
    stripes (one absent bar makes a single 2x gap; the median ignores it).

The boundary's leading edge sits an arbitrary fraction of a pitch before the
first stripe, and that fraction moves as the detected polygon tightens and
loosens. Snapping to the phase of the detections themselves cancels it out.

Known limit: if the boundary changes by a *whole* pitch, every index shifts by
one, because that is genuinely indistinguishable from a crosswalk with one more
stripe at its leading edge. Sub-pitch noise — the realistic case — is handled.
Consumers should treat the index as a stable relative position rather than an
absolute anchor.

Indexes are per segment and 0-based. Gaps in the sequence are meaningful: they
are stripes the model could not see this run.
"""

import math
import statistics
from typing import Any

Point = tuple[float, float]
Polygon = list[list[float]]

# Below this many detections there is no meaningful gap to take a median of,
# so the pitch is estimated from the boundary extent instead.
MIN_STRIPES_FOR_PITCH = 3


def centroid(polygon: Polygon) -> Point:
    """Arithmetic mean of the vertices — good enough for thin stripe quads."""
    if not polygon:
        return (0.0, 0.0)
    return (
        sum(p[0] for p in polygon) / len(polygon),
        sum(p[1] for p in polygon) / len(polygon),
    )


def principal_axis(polygon: Polygon) -> Point:
    """Unit vector along the polygon's longest dimension.

    The first principal component of the vertex cloud, via the closed-form
    eigenvector of the 2x2 covariance matrix. For a crosswalk boundary this is
    the direction pedestrians walk across, which is the axis stripes are
    spaced along.
    """
    if len(polygon) < 2:
        return (1.0, 0.0)

    mean_x, mean_y = centroid(polygon)
    n = len(polygon)
    cxx = sum((p[0] - mean_x) ** 2 for p in polygon) / n
    cyy = sum((p[1] - mean_y) ** 2 for p in polygon) / n
    cxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in polygon) / n

    # Largest eigenvalue of [[cxx, cxy], [cxy, cyy]].
    trace = cxx + cyy
    diff = math.sqrt((cxx - cyy) ** 2 + 4 * cxy**2)
    eigenvalue = (trace + diff) / 2

    # Eigenvector for that eigenvalue. When cxy is ~0 the axes are already
    # aligned, so pick whichever of x/y carries more variance.
    if abs(cxy) < 1e-9:
        return (1.0, 0.0) if cxx >= cyy else (0.0, 1.0)

    vx, vy = cxy, eigenvalue - cxx
    length = math.hypot(vx, vy)
    if length < 1e-9:
        return (1.0, 0.0)
    return (vx / length, vy / length)


def project(point: Point, axis: Point) -> float:
    """Scalar projection of a point onto an axis direction."""
    return point[0] * axis[0] + point[1] * axis[1]


def estimate_pitch(projections: list[float], span: float) -> float:
    """Distance between adjacent stripes, robust to missing ones.

    Uses the median consecutive gap. A stripe hidden by a vehicle turns one
    gap into roughly 2x pitch, which the median discards as long as fewer than
    half the stripes are missing. Falls back to spreading the detections
    evenly across the boundary span when there are too few to measure.
    """
    ordered = sorted(projections)

    if len(ordered) >= MIN_STRIPES_FOR_PITCH:
        gaps = [b - a for a, b in zip(ordered, ordered[1:]) if b - a > 1e-6]
        if gaps:
            return statistics.median(gaps)

    if len(ordered) >= 2:
        measured = ordered[-1] - ordered[0]
        if measured > 1e-6:
            return measured / (len(ordered) - 1)

    # A single detection tells us nothing about spacing; any positive pitch
    # puts it at a sane index, so use the whole span.
    return span if span > 1e-6 else 1.0


def index_stripes(boundary: Polygon, stripes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign each stripe an integer slot index along the crosswalk.

    `stripes` entries need a "polygon" key. Returns them with "stripeIndex"
    added, ordered by that index. Stripes are indexed from the boundary's
    leading edge, so the index reflects physical position rather than
    detection order.
    """
    if not stripes:
        return []

    centroids = [centroid(s["polygon"]) for s in stripes]

    # The axis and origin come from the boundary so they stay put when
    # detections come and go. Without a usable boundary, fall back to the
    # stripes' own centroid cloud.
    has_boundary = bool(boundary) and len(boundary) >= 3
    axis = principal_axis(boundary if has_boundary else [[c[0], c[1]] for c in centroids])

    projections = [project(c, axis) for c in centroids]

    if has_boundary:
        edges = [project((p[0], p[1]), axis) for p in boundary]
        origin, span = min(edges), max(edges) - min(edges)
    else:
        origin, span = min(projections), max(projections) - min(projections)

    pitch = estimate_pitch(projections, span)

    # Slot positions before phase correction. These are anchored to the
    # boundary edge, which sits an arbitrary fraction of a pitch before the
    # first stripe — and that fraction wobbles as the boundary polygon
    # tightens and loosens between runs.
    raw = [(p - origin) / pitch for p in projections]

    # Snapping to the detections' own lattice removes that wobble: the phase
    # is a property of the stripe spacing, not of which stripes happen to be
    # visible, so it lands in the same place when the leading bars are hidden.
    phase = _estimate_phase(raw)

    indexed = [
        {**stripe, "stripeIndex": max(0, _round_half_up(value - phase))}
        for stripe, value in zip(stripes, raw)
    ]

    indexed.sort(key=lambda s: s["stripeIndex"])
    return _dedupe_indexes(indexed)


def _round_half_up(value: float) -> int:
    """Round .5 away from zero.

    Python's built-in round() is banker's rounding, which sends 0.5 and 1.5
    to 0 and 2 — evenly spaced stripes landing exactly on half-slots would
    collide in pairs.
    """
    return int(math.floor(value + 0.5))


def _estimate_phase(raw: list[float]) -> float:
    """Fractional offset of the stripe lattice, as a circular mean.

    Averaging the fractional parts directly breaks when they straddle the
    0/1 wrap (0.98 and 0.02 average to 0.5 rather than 0.0), so treat each
    fraction as an angle and average on the circle instead.
    """
    if not raw:
        return 0.0

    angles = [2 * math.pi * (value % 1.0) for value in raw]
    mean_sin = sum(math.sin(a) for a in angles) / len(angles)
    mean_cos = sum(math.cos(a) for a in angles) / len(angles)

    # Fractions spread evenly around the circle cancel out and leave no
    # meaningful phase — keep the boundary anchor in that case.
    if math.hypot(mean_sin, mean_cos) < 1e-9:
        return 0.0

    return (math.atan2(mean_sin, mean_cos) / (2 * math.pi)) % 1.0


def _dedupe_indexes(stripes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure indexes are strictly increasing.

    Two detections can round into the same slot when the pitch estimate is
    slightly short. Rather than drop one, push the later stripe to the next
    free slot — it keeps every detection and preserves left-to-right order.
    """
    seen: set[int] = set()
    for stripe in stripes:
        index = stripe["stripeIndex"]
        while index in seen:
            index += 1
        stripe["stripeIndex"] = index
        seen.add(index)
    return stripes


def assign_segments(
    detections: list[dict[str, Any]],
    boundaries: dict[str, Polygon],
) -> dict[str, list[dict[str, Any]]]:
    """Group detections by which crosswalk boundary they fall in.

    Replaces the old hardcoded pixel ranges for View 5056's bollard median.
    A detection inside a boundary belongs to it; one outside every boundary
    goes to the nearest by centroid distance, which keeps stripes whose paint
    spills past a tight boundary polygon.
    """
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in boundaries}
    usable = {name: polygon for name, polygon in boundaries.items() if polygon and len(polygon) >= 3}
    if not usable:
        return grouped

    for detection in detections:
        point = (detection["x"], detection["y"])

        containing = [name for name, polygon in usable.items() if point_in_polygon(point, polygon)]
        if len(containing) == 1:
            grouped[containing[0]].append(detection)
            continue

        candidates = containing or list(usable)
        nearest = min(
            candidates,
            key=lambda name: _squared_distance(point, centroid(usable[name])),
        )
        grouped[nearest].append(detection)

    return grouped


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Standard ray-casting test."""
    x, y = point
    inside = False
    previous = len(polygon) - 1
    for current in range(len(polygon)):
        cx, cy = polygon[current][0], polygon[current][1]
        px, py = polygon[previous][0], polygon[previous][1]
        if (cy > y) != (py > y) and x < (px - cx) * (y - cy) / (py - cy) + cx:
            inside = not inside
        previous = current
    return inside


def _squared_distance(a: Point, b: Point) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
