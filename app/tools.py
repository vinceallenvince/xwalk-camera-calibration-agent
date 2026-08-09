"""ADK tools for the calibration agent.

Two tools, each doing what it is good at:

  analyse_conditions  — Gemini Flash classifies the frame and produces
                        status / conditions / reasoning (language).
  detect_stripes      — Roboflow workflow returns paint-accurate instance
                        segmentation polygons (geometry).

The agent decides whether to call detect_stripes based on the conditions result.
"""

import base64
import json
import os
import statistics
from typing import Any

import httpx
from google import genai
from google.genai import types

from app.reference import REFERENCE_CALIBRATION, STRIPE_COUNT

# ---------------------------------------------------------------------------
# Gemini — conditions triage
# ---------------------------------------------------------------------------

FLASH_MODEL = os.environ.get("CALIBRATION_TRIAGE_MODEL", "gemini-2.5-flash")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "xwalk-keyboards-01")
REASONING_MAX_CHARS = 250

_CONDITIONS_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["status", "conditions", "confidence", "reasoning"],
    "properties": {
        "status": {
            "type": "STRING",
            "enum": ["ok", "degraded", "no_crosswalk", "feed_down", "needs_review"],
        },
        "conditions": {
            "type": "OBJECT",
            "required": ["crosswalkVisible", "obstruction", "cameraMoved", "repaintSuspected"],
            "properties": {
                "crosswalkVisible": {"type": "BOOLEAN"},
                "obstruction": {
                    "type": "STRING",
                    "enum": ["none", "snow", "vehicle", "construction", "glare", "darkness", "other"],
                },
                "cameraMoved": {"type": "STRING", "enum": ["none", "slight", "significant"]},
                "repaintSuspected": {"type": "BOOLEAN"},
            },
        },
        "confidence": {"type": "NUMBER"},
        "reasoning": {"type": "STRING"},
    },
}

_CONDITIONS_INSTRUCTION = f"""\
You are a traffic camera analyst for 511NY View 5056 (West Street at W. 34 St,
Manhattan). This camera shows two crosswalks separated by a bollard median.

Given a current frame, classify its condition:

status:
  ok            — both crosswalks are clearly visible, normal conditions
  degraded      — crosswalks are visible but partially obstructed or image quality
                  is reduced (vehicles, rain, glare, darkness)
  no_crosswalk  — the camera has been re-aimed away from the intersection, or the
                  crosswalks are completely invisible (whiteout, total obstruction)
  feed_down     — this is a known outage placeholder image (text saying "camera
                  being serviced" or "no live camera feed"), NOT a real camera frame
  needs_review  — the camera appears to have been significantly repositioned, or
                  the crosswalk has been repainted/reconfigured

conditions.crosswalkVisible: can you see painted crosswalk stripes at all?
conditions.obstruction: the dominant thing degrading the view, if any.
conditions.cameraMoved: "none" if the view is normal, "slight" for ordinary
  thermal drift, "significant" for an apparent physical re-aim.
conditions.repaintSuspected: true if the paint looks freshly re-striped.

confidence: 0.0-1.0, your honest confidence in the classification.

reasoning: why this status. MAXIMUM {REASONING_MAX_CHARS} CHARACTERS. Specific
and terse. Operators and a dashboard read this.

Be conservative: prefer "degraded" over "ok" when uncertain, and "needs_review"
over "ok" when the camera appears moved significantly.
"""

_GEMINI_CLIENT: genai.Client | None = None


def _gemini() -> genai.Client:
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        credentials = None
        token = os.environ.get("CALIBRATION_ACCESS_TOKEN")
        if token:
            from google.oauth2.credentials import Credentials
            credentials = Credentials(token=token)
        _GEMINI_CLIENT = genai.Client(
            vertexai=True, project=PROJECT, location=LOCATION, credentials=credentials,
        )
    return _GEMINI_CLIENT


def analyse_conditions(image_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
    """Classify the frame's condition using Gemini Flash."""
    response = _gemini().models.generate_content(
        model=FLASH_MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    types.Part.from_text(text="Classify this camera frame."),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=_CONDITIONS_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=_CONDITIONS_SCHEMA,
            temperature=0.0,
        ),
    )
    result = json.loads(response.text)

    reasoning = result.get("reasoning") or ""
    if len(reasoning) > REASONING_MAX_CHARS:
        result["reasoning"] = reasoning[: REASONING_MAX_CHARS - 1].rstrip() + "…"

    usage = getattr(response, "usage_metadata", None)
    result["_usage"] = {
        "promptTokens": getattr(usage, "prompt_token_count", None),
        "responseTokens": getattr(usage, "candidates_token_count", None),
        "totalTokens": getattr(usage, "total_token_count", None),
    } if usage else None
    result["_model"] = FLASH_MODEL
    return result


# ---------------------------------------------------------------------------
# Roboflow — stripe detection
# ---------------------------------------------------------------------------

ROBOFLOW_API_KEY = os.environ.get("ROBOFLOW_API_KEY", "")
STRIPE_WORKFLOW_URL = os.environ.get(
    "CALIBRATION_STRIPE_WORKFLOW_URL",
    "https://serverless.roboflow.com/vince-vinceallen-com/workflows/crosswalk-stripe-detection-1786299496725",
)
BOUNDARY_WORKFLOW_URL = os.environ.get(
    "CALIBRATION_BOUNDARY_WORKFLOW_URL",
    "https://serverless.roboflow.com/vince-vinceallen-com/workflows/crosswalk-boundary-detection-1786302696881",
)
MEDIAN_GAP = (175, 280)
BOUNDARY_MIN_CONFIDENCE = 0.8
BOUNDARY_MIN_WIDTH = 30
MAX_WIDTH = 50
MIN_HEIGHT = 5
MIN_CONFIDENCE = 0.7
MIN_CENTROID_DIST = 5.0


def _dedup(detections: list[dict]) -> list[dict]:
    detections = sorted(detections, key=lambda d: -d.get("confidence", 0))
    kept: list[dict] = []
    for det in detections:
        cx, cy = det["x"], det["y"]
        if any(((cx - k["x"]) ** 2 + (cy - k["y"]) ** 2) ** 0.5 < MIN_CENTROID_DIST for k in kept):
            continue
        kept.append(det)
    return sorted(kept, key=lambda d: d["x"])


def detect_boundaries(image_bytes: bytes) -> dict[str, Any]:
    """Detect the left and right crosswalk boundary polygons using Roboflow."""
    b64 = base64.b64encode(image_bytes).decode()

    with httpx.Client(timeout=30) as client:
        resp = client.post(BOUNDARY_WORKFLOW_URL, json={
            "api_key": ROBOFLOW_API_KEY,
            "inputs": {"image": {"type": "base64", "value": b64}},
        })
        resp.raise_for_status()

    output = resp.json()["outputs"][0]
    preds = output["predictions"].get("predictions", [])

    # Keep only high-confidence, large detections (the two real crosswalks).
    real = sorted(
        [p for p in preds if p.get("confidence", 0) >= BOUNDARY_MIN_CONFIDENCE and p.get("width", 0) >= BOUNDARY_MIN_WIDTH],
        key=lambda p: p["x"],
    )

    left_polygon: list[list[float]] = []
    right_polygon: list[list[float]] = []

    for det in real:
        polygon = [[pt["x"], pt["y"]] for pt in det.get("points", [])]
        if det["x"] < MEDIAN_GAP[0]:
            left_polygon = polygon
        elif det["x"] > MEDIAN_GAP[1]:
            right_polygon = polygon

    return {
        "leftCrosswalk": left_polygon,
        "rightCrosswalk": right_polygon,
        "raw_count": len(preds),
        "filtered_count": len(real),
    }


def detect_stripes(image_bytes: bytes) -> dict[str, Any]:
    """Detect crosswalk stripes using the Roboflow workflow and match to reference."""
    b64 = base64.b64encode(image_bytes).decode()

    with httpx.Client(timeout=30) as client:
        resp = client.post(STRIPE_WORKFLOW_URL, json={
            "api_key": ROBOFLOW_API_KEY,
            "inputs": {"image": {"type": "base64", "value": b64}},
        })
        resp.raise_for_status()

    output = resp.json()["outputs"][0]
    preds = output["predictions"]
    raw = preds.get("predictions", [])

    filtered = [
        p for p in raw
        if p.get("width", 0) < MAX_WIDTH
        and p.get("height", 0) >= MIN_HEIGHT
        and p.get("confidence", 0) >= MIN_CONFIDENCE
    ]
    clean = _dedup(filtered)

    left = [d for d in clean if d["x"] < MEDIAN_GAP[0]]
    right = [d for d in clean if d["x"] > MEDIAN_GAP[1]]

    ref_left = [s for s in REFERENCE_CALIBRATION["stripes"] if s["segment"] == "left"]
    ref_right = [s for s in REFERENCE_CALIBRATION["stripes"] if s["segment"] == "right"]

    stripes: list[dict[str, Any]] = []
    notes: list[str] = []
    confidences: list[float] = []

    for seg_name, detected, reference in [("left", left, ref_left), ("right", right, ref_right)]:
        if len(detected) != len(reference):
            notes.append(f"{seg_name}: detected {len(detected)}, reference expects {len(reference)}")
        for i, ref in enumerate(reference):
            if i < len(detected):
                det = detected[i]
                polygon = [[pt["x"], pt["y"]] for pt in det.get("points", [])]
                conf = det.get("confidence", 0)
                confidences.append(conf)
                stripes.append({
                    "stripeIndex": ref["stripeIndex"],
                    "segment": seg_name,
                    "note": ref["note"],
                    "visible": True,
                    "polygon": polygon,
                    "confidence": conf,
                })
            else:
                stripes.append({
                    "stripeIndex": ref["stripeIndex"],
                    "segment": seg_name,
                    "note": ref["note"],
                    "visible": False,
                    "polygon": [],
                })

    stripes.sort(key=lambda s: s["stripeIndex"])
    visible = sum(1 for s in stripes if s["visible"])

    return {
        "stripes": stripes,
        "matching": {
            "counts": {"left": len(left), "right": len(right)},
            "notes": notes,
            "clean": not notes,
        },
        "stripe_count": len(clean),
        "visible_count": visible,
        "expected_count": STRIPE_COUNT,
        "count_match": not notes,
        "max_confidence": max(confidences) if confidences else 0,
        "min_confidence": min(confidences) if confidences else 0,
        "mean_confidence": round(statistics.mean(confidences), 4) if confidences else 0,
        "raw_count": len(raw),
        "referenceFrame": preds.get("image", {}),
    }
