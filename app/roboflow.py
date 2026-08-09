"""Roboflow-based stripe detection.

Calls the Crosswalk Stripe Detection workflow, which returns instance
segmentation polygons for each painted bar. These are the actual paint outlines
— not cells — so the web client's `isPointInPolygon` test fires when a
pedestrian's foot is on the paint, which is the most visually honest trigger.

The raw detections need light post-processing:
  1. Drop the occasional mega-blob (a detection spanning the entire band).
  2. Drop tiny fragments (height < 5px, usually edge artifacts).
  3. Deduplicate overlapping detections of the same bar (soft-NMS by IoU).
  4. Segment into left / right by the bollard-median gap.
  5. Match to reference stripes by left-to-right order within each segment.
"""

import json
import os
from typing import Any

import httpx

from app.reference import REFERENCE_CALIBRATION, STRIPE_COUNT

ROBOFLOW_API_KEY = os.environ.get("ROBOFLOW_API_KEY", "")
WORKFLOW_URL = os.environ.get(
    "CALIBRATION_WORKFLOW_URL",
    "https://serverless.roboflow.com/vince-vinceallen-com/workflows/crosswalk-stripe-detection-1786299496725",
)

# The bollard median sits between x~175 and x~280 in the 352×240 frame.
MEDIAN_GAP = (175, 280)

# Post-processing thresholds.
MAX_WIDTH = 50       # anything wider is a mega-blob, not a single stripe
MIN_HEIGHT = 5       # fragments shorter than this are edge noise
MIN_CONFIDENCE = 0.7
IOU_THRESHOLD = 0.3  # overlapping detections above this are the same bar


def _bbox_iou(a: dict, b: dict) -> float:
    """Axis-aligned bounding-box IoU from the detection's x/y/w/h."""
    ax1 = a["x"] - a["width"] / 2
    ay1 = a["y"] - a["height"] / 2
    ax2 = a["x"] + a["width"] / 2
    ay2 = a["y"] + a["height"] / 2
    bx1 = b["x"] - b["width"] / 2
    by1 = b["y"] - b["height"] / 2
    bx2 = b["x"] + b["width"] / 2
    by2 = b["y"] + b["height"] / 2
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = a["width"] * a["height"] + b["width"] * b["height"] - inter
    return inter / union if union > 0 else 0


def _dedup(detections: list[dict], min_centroid_dist: float = 5.0) -> list[dict]:
    """Deduplicate by centroid distance rather than bbox IoU.

    Near-field stripes have bounding boxes that overlap substantially (IoU 0.3+)
    even when they are genuinely separate bars, because perspective makes them
    wide and close together. Centroid distance is a more reliable discriminator:
    two detections of the same bar produce nearly the same centroid, while
    adjacent bars are at least one bar-width apart (~8px at the closest).
    """
    detections = sorted(detections, key=lambda d: -d.get("confidence", 0))
    kept: list[dict] = []
    for det in detections:
        cx, cy = det["x"], det["y"]
        if any(((cx - k["x"]) ** 2 + (cy - k["y"]) ** 2) ** 0.5 < min_centroid_dist for k in kept):
            continue
        kept.append(det)
    return sorted(kept, key=lambda d: d["x"])


def _segment(detections: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into left / right by the median gap."""
    left = [d for d in detections if d["x"] < MEDIAN_GAP[0]]
    right = [d for d in detections if d["x"] > MEDIAN_GAP[1]]
    return left, right


def detect(image_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
    """Call the Roboflow workflow and return cleaned, matched detections."""
    import base64

    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "api_key": ROBOFLOW_API_KEY,
        "inputs": {"image": {"type": "base64", "value": b64}},
    }

    with httpx.Client(timeout=30) as client:
        response = client.post(WORKFLOW_URL, json=payload)
        response.raise_for_status()

    data = response.json()
    output = data["outputs"][0]
    preds = output["predictions"]
    image_info = preds.get("image", {})
    raw = preds.get("predictions", [])

    # 1–3: filter and deduplicate
    filtered = [
        p for p in raw
        if p.get("width", 0) < MAX_WIDTH
        and p.get("height", 0) >= MIN_HEIGHT
        and p.get("confidence", 0) >= MIN_CONFIDENCE
    ]
    clean = _dedup(filtered)

    # 4: segment
    left, right = _segment(clean)

    # 5: match to reference by order
    ref_left = [s for s in REFERENCE_CALIBRATION["stripes"] if s["segment"] == "left"]
    ref_right = [s for s in REFERENCE_CALIBRATION["stripes"] if s["segment"] == "right"]

    stripes: list[dict[str, Any]] = []
    notes: list[str] = []

    for segment_name, detected, reference in [("left", left, ref_left), ("right", right, ref_right)]:
        if len(detected) != len(reference):
            notes.append(f"{segment_name}: detected {len(detected)}, reference expects {len(reference)}")

        for i, ref in enumerate(reference):
            if i < len(detected):
                det = detected[i]
                polygon = [[pt["x"], pt["y"]] for pt in det.get("points", [])]
                stripes.append({
                    "stripeIndex": ref["stripeIndex"],
                    "segment": segment_name,
                    "note": ref["note"],
                    "visible": True,
                    "polygon": polygon,
                    "confidence": det.get("confidence"),
                })
            else:
                stripes.append({
                    "stripeIndex": ref["stripeIndex"],
                    "segment": segment_name,
                    "note": ref["note"],
                    "visible": False,
                    "polygon": [],
                    "reason": "no detection aligned to this reference stripe",
                })

    stripes.sort(key=lambda s: s["stripeIndex"])

    # Derive status
    visible = sum(1 for s in stripes if s["visible"])
    clean_match = not notes
    if visible < STRIPE_COUNT * 0.5:
        status = "degraded"
    elif not clean_match:
        status = "needs_review"
    else:
        status = "ok"

    return {
        "status": status,
        "reasoning": f"{visible}/{STRIPE_COUNT} stripes detected in {len(raw)} raw predictions. "
                     + (f"Count mismatch: {'; '.join(notes)}." if notes else "Counts match reference."),
        "conditions": {
            "crosswalkVisible": visible > 0,
            "obstruction": "none",
            "cameraMoved": "none",
            "repaintSuspected": False,
        },
        "confidence": min(s.get("confidence", 0) for s in stripes if s["visible"]) if visible else 0,
        "referenceFrame": image_info,
        "stripes": stripes,
        "matching": {"counts": {"left": len(left), "right": len(right)}, "notes": notes, "clean": clean_match},
        "raw_count": len(raw),
        "filtered_count": len(clean),
    }
