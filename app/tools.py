"""ADK tools for the calibration agent.

Three tools, each doing what it is good at:

  analyse_conditions  — Gemini Flash classifies the frame and produces
                        status / conditions / reasoning (language).
  detect_boundaries   — Roboflow workflow returns the crosswalk outlines,
                        named left-to-right.
  detect_stripes      — Roboflow workflow returns paint-accurate instance
                        segmentation polygons, placed along those crosswalks.

The agent decides whether to run detection based on the conditions result.

Nothing here is baked to a camera. Triage takes the camera's registered scene
description as input (see app/cameras.py), crosswalks are named by their order
in the frame, stripes are indexed by position along their crosswalk, and no
reference calibration is consulted — so pointing this at a new camera needs no
hand-authored geometry.
"""

import base64
import json
import os
import statistics
from typing import Any

import httpx
from google import genai
from google.genai import types

from app.cameras import CameraConfig
from app.geometry import assign_segments, index_stripes

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
            "enum": ["ok", "degraded", "no_crosswalk", "feed_down"],
        },
        "conditions": {
            "type": "OBJECT",
            "required": ["crosswalkVisible", "occlusion", "visibility", "cameraMoved", "repaintSuspected"],
            "properties": {
                "crosswalkVisible": {"type": "BOOLEAN"},
                "occlusion": {
                    "type": "STRING",
                    "enum": ["none", "vehicle", "construction", "snow_cover", "debris", "other"],
                },
                "visibility": {
                    "type": "STRING",
                    "enum": ["clear", "shadows", "dusk", "glare", "rain", "snowfall", "fog", "dark", "other"],
                },
                "cameraMoved": {"type": "STRING", "enum": ["none", "slight", "significant"]},
                "repaintSuspected": {"type": "BOOLEAN"},
            },
        },
        "confidence": {"type": "NUMBER"},
        "reasoning": {"type": "STRING"},
    },
}

def conditions_instruction(camera: CameraConfig) -> str:
    """The triage system prompt, specialized to one camera's scene.

    The scene description is the yardstick the model classifies against — a
    frame is "ok" when it matches the registered scene, "degraded" when it
    doesn't. Judging a new camera against another camera's scene would
    flag a perfectly good view, which is why this is per-camera config
    rather than a module constant.
    """
    return f"""\
You are a traffic camera analyst for {camera.name}.
{camera.scene}

Given a current frame, classify its condition:

status:
  ok            — the crosswalks in view are clearly visible, normal conditions
  degraded      — crosswalks are visible but conditions are reduced: partial
                  occlusion, shadows across the paint, dusk light, glare, rain,
                  or a view that does not match the scene described above
  no_crosswalk  — no painted crosswalk is visible at all: the camera is aimed
                  somewhere without a crosswalk in frame, or a whiteout/total
                  obstruction hides everything
  feed_down     — this is a known outage placeholder image (text saying "camera
                  being serviced" or "no live camera feed"), NOT a real camera frame

If the camera appears re-aimed but painted crosswalks are still visible, report
"degraded" with cameraMoved "significant" and explain in reasoning — do NOT
report "no_crosswalk" while paint is in view.

conditions.crosswalkVisible: can you see painted crosswalk stripes at all?

conditions.occlusion: the dominant thing physically covering any of the painted
  stripes, if any. A vehicle on the paint — stopped or passing — is "vehicle";
  snow lying on the paint is "snow_cover". Report it even when status is "ok":
  a single car crossing the paint in an otherwise normal frame is occlusion
  "vehicle" with status "ok".

conditions.visibility: the dominant lighting or atmospheric factor reducing
  paint contrast, judged independently of occlusion:
    clear    — paint contrast is good; a streetlit night scene with crisp
               stripes is "clear", not "dark"
    shadows  — strong shadows fall across the crosswalk (trees, buildings,
               low-angle sun); these cut stripe contrast even on sunny days
    dusk     — twilight: the sun is down or nearly down and street lighting
               is not yet dominant; the scene is flat and low-contrast
    glare    — sun or headlights washing out part of the view
    rain / snowfall / fog — precipitation or haze degrading the image
    dark     — genuinely underlit; stripes barely distinguishable
  Report the factor even when status is "ok" — this field feeds a dashboard
  correlating conditions with detection quality.

conditions.cameraMoved: "none" if the view is normal, "slight" for ordinary
  thermal drift, "significant" for an apparent physical re-aim.

conditions.repaintSuspected: true if the paint looks freshly re-striped.

confidence: 0.0-1.0, your honest confidence in the classification.

reasoning: why this status. MAXIMUM {REASONING_MAX_CHARS} CHARACTERS. Specific
and terse. Operators and a dashboard read this.

Be conservative: prefer "degraded" over "ok" when uncertain, especially when
shadows or dusk light fall on the painted area.
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


def analyse_conditions(
    image_bytes: bytes,
    camera: CameraConfig,
    mime_type: str = "image/png",
) -> dict[str, Any]:
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
            system_instruction=conditions_instruction(camera),
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
BOUNDARY_MIN_CONFIDENCE = 0.8
BOUNDARY_MIN_WIDTH = 30
MAX_WIDTH = 50
MIN_HEIGHT = 5
MIN_CONFIDENCE = 0.5
MIN_CENTROID_DIST = 5.0
MIN_POLYGON_POINTS = 3

# Segment names, assigned in left-to-right order of the detected crosswalk
# boundaries. Extra crosswalks beyond these fall back to "segment3", "segment4"…
SEGMENT_NAMES = ("left", "right")


def _dedup(detections: list[dict]) -> list[dict]:
    detections = sorted(detections, key=lambda d: -d.get("confidence", 0))
    kept: list[dict] = []
    for det in detections:
        cx, cy = det["x"], det["y"]
        if any(((cx - k["x"]) ** 2 + (cy - k["y"]) ** 2) ** 0.5 < MIN_CENTROID_DIST for k in kept):
            continue
        kept.append(det)
    return sorted(kept, key=lambda d: d["x"])


def _segment_name(position: int) -> str:
    """Name the nth crosswalk, counting from the left of the frame."""
    if position < len(SEGMENT_NAMES):
        return SEGMENT_NAMES[position]
    return f"segment{position + 1}"


def largest_boundaries(detections: list[dict], count: int | None) -> list[dict]:
    """Keep the `count` largest detections by bounding-box area, in x order.

    Occlusion can split one crosswalk into multiple detected boundaries — a
    truck parked mid-crosswalk leaves two disconnected patches of paint, and
    each becomes its own detection. When the camera's real crosswalk count is
    known, everything beyond it is a fragment: keep the largest boundaries
    (a fragment is always smaller than the crosswalk it broke off of) and
    drop the rest, so a phantom segment can never be published.
    """
    if count is None or len(detections) <= count:
        return detections
    kept = sorted(detections, key=lambda d: -(d.get("width", 0) * d.get("height", 0)))[:count]
    return sorted(kept, key=lambda d: d["x"])


def detect_boundaries(image_bytes: bytes, max_crosswalks: int | None = None) -> dict[str, Any]:
    """Detect the crosswalk boundary polygons using Roboflow.

    Boundaries are named by their left-to-right order in the frame rather than
    by fixed pixel ranges, so this works on any camera regardless of where the
    crosswalks sit or how many there are. `max_crosswalks` is the camera's
    registered crosswalk count — see largest_boundaries.

    Why the stripe pass wants boundaries at all: the boundary provides an
    origin that doesn't move when leading stripes are hidden, and a partition
    when the gap between crosswalks is occluded. Both are memory of what the
    crosswalk looks like when you can't currently see all of it — everything
    else in geometry.py can be recovered from the visible stripes alone.
    """
    b64 = base64.b64encode(image_bytes).decode()

    with httpx.Client(timeout=30) as client:
        resp = client.post(BOUNDARY_WORKFLOW_URL, json={
            "api_key": ROBOFLOW_API_KEY,
            "inputs": {"image": {"type": "base64", "value": b64}},
        })
        resp.raise_for_status()

    output = resp.json()["outputs"][0]
    preds = output["predictions"].get("predictions", [])

    # Keep only high-confidence, large detections (the real crosswalks, not
    # painted fragments elsewhere in the frame).
    real = sorted(
        [p for p in preds if p.get("confidence", 0) >= BOUNDARY_MIN_CONFIDENCE and p.get("width", 0) >= BOUNDARY_MIN_WIDTH],
        key=lambda p: p["x"],
    )
    real = largest_boundaries(real, max_crosswalks)

    crosswalks: dict[str, list[list[float]]] = {}
    for position, det in enumerate(real):
        polygon = [[pt["x"], pt["y"]] for pt in det.get("points", [])]
        if len(polygon) >= MIN_POLYGON_POINTS:
            crosswalks[_segment_name(position)] = polygon

    return {
        "crosswalks": crosswalks,
        # Flattened aliases for the published schema, which names the first two.
        "leftCrosswalk": crosswalks.get("left", []),
        "rightCrosswalk": crosswalks.get("right", []),
        "raw_count": len(preds),
        "filtered_count": len(real),
    }


def detect_stripes(
    image_bytes: bytes,
    boundaries: dict[str, list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Detect crosswalk stripes and place each one along its crosswalk.

    Every stripe Roboflow returns is published. There is no reference
    calibration to match against and no expected count to satisfy — the agent
    reports the geometry it can see, and the client decides what to do with it.

    Each stripe carries a `stripeIndex`: its slot position measured from the
    crosswalk's leading edge in units of the stripe pitch. The index is
    positional, not ordinal, so a stripe keeps the same index when a vehicle
    hides its neighbours. Missing indexes mean stripes the model could not see.

    Notes are deliberately absent. The stripe index is the whole identity; the
    client owns the mapping from index to pitch.
    """
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

    crosswalks = boundaries or {}
    grouped = (
        assign_segments(clean, crosswalks)
        if crosswalks
        # With no boundary to group by, the whole frame is one crosswalk.
        else {"left": clean}
    )

    stripes: list[dict[str, Any]] = []
    confidences: list[float] = []
    segment_counts: dict[str, int] = {}

    for name, detections in grouped.items():
        candidates = []
        for det in detections:
            polygon = [[pt["x"], pt["y"]] for pt in det.get("points", [])]
            if len(polygon) < MIN_POLYGON_POINTS:
                continue
            confidence = det.get("confidence", 0)
            confidences.append(confidence)
            candidates.append({
                "segment": name,
                "polygon": polygon,
                "confidence": confidence,
            })

        segment_counts[name] = len(candidates)
        stripes.extend(index_stripes(crosswalks.get(name, []), candidates))

    stripes.sort(key=lambda s: (s["segment"], s["stripeIndex"]))

    return {
        "stripes": stripes,
        "segments": segment_counts,
        "stripe_count": len(clean),
        "visible_count": len(stripes),
        "max_confidence": max(confidences) if confidences else 0,
        "min_confidence": min(confidences) if confidences else 0,
        "mean_confidence": round(statistics.mean(confidences), 4) if confidences else 0,
        "raw_count": len(raw),
        "referenceFrame": preds.get("image", {}),
    }
