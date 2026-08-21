"""Camera-agnostic geometry for turning raw detections into stripe positions.

Nothing here knows about a particular camera, musical notes, or how many
stripes a crosswalk "should" have. Given a set of detected stripe polygons, it
answers one question: where does each stripe sit along the crosswalk?

Glossary:

  principal axis — the direction the stripe centroids are spread; the axis the
                   crosswalk runs along.
  projection     — a stripe centroid's scalar position along that axis.
  segment        — which crosswalk run a stripe belongs to. Named positionally
                   along the axis: "segment0", "segment1", ...
  stripeIndex    — the stripe's ordinal position within its segment. Integer,
                   0-based, contiguous.

The heuristic in one pass: flatten each stripe polygon to its centroid,
project the centroids onto the principal axis, sort, start a new segment
wherever the gap to the next stripe exceeds SEGMENT_GAP_FRACTION of the frame
width, and number the stripes within each segment in order.

Indexes are ordinal, not positional. A stripe the model could not see this run
does not leave a hole — its neighbours simply renumber. Identities therefore
wobble between runs, which is a deliberate trade (VIN-44): the client anchors
notes from a per-camera base rather than pinning a stripe to a fixed pitch, so
a renumbering transposes the scale rather than corrupting it. Nothing here
carries memory between runs.

Why a gap threshold, and why a fraction of frame width: the separation between
crosswalk runs is far larger than the spacing between stripes within one run
(on both registered cameras, medians of ~136px and ~9px in a 352px frame). A
threshold relative to the *median stripe gap* was tried and rejected — sparse
reads inflate that median and balloon the threshold, so it under-splits exactly
when detection is worst. Replayed against 341 archived runs across both
cameras, a frame-width fraction agrees with the retired boundary pipeline on
86% (5056) and 90% (5072) of runs, and the plateau spans 0.20-0.35 on both,
so the constant is not knife-edge.
"""

import math
from typing import Any

Point = tuple[float, float]
Polygon = list[list[float]]

# Start a new segment when the gap between consecutive stripes along the axis
# exceeds this fraction of the frame width. Centre of the plateau that serves
# both registered cameras; see the module docstring.
SEGMENT_GAP_FRACTION = 0.25

# Used when a caller cannot supply a frame width. Both registered cameras are
# 352px wide; a wrong guess only shifts where segments split.
FALLBACK_FRAME_WIDTH = 352.0

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


def segment_name(position: int) -> str:
    """Name the nth crosswalk run, counting along the principal axis."""
    return f"segment{position}"


def place_stripes(
    stripes: list[dict[str, Any]],
    frame_width: float | None = None,
) -> list[dict[str, Any]]:
    """Assign each stripe a segment and an ordinal index along the crosswalk.

    `stripes` entries need a "polygon" key. Returns them with "segment" and
    "stripeIndex" added, ordered along the principal axis. Segments are named
    positionally, so "segment0" is always the first run along that axis.
    """
    if not stripes:
        return []

    centroids = [centroid(s["polygon"]) for s in stripes]
    axis = principal_axis([[c[0], c[1]] for c in centroids])
    projections = [project(c, axis) for c in centroids]

    threshold = SEGMENT_GAP_FRACTION * (frame_width or FALLBACK_FRAME_WIDTH)

    placed: list[dict[str, Any]] = []
    segment, index, previous = 0, 0, None

    for position in sorted(range(len(stripes)), key=lambda i: projections[i]):
        projection = projections[position]
        if previous is not None and projection - previous > threshold:
            segment += 1
            index = 0
        placed.append({
            **stripes[position],
            "segment": segment_name(segment),
            "stripeIndex": index,
        })
        index += 1
        previous = projection

    return placed
