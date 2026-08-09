"""The calibration agent.

Given a current frame of View 5056, relocate the crosswalk polygons and the
striped keyboard. This is a *relocation* task, not a discovery task: the
reference calibration is supplied on every call and owns the stripe count,
order, segment, and note bindings. The model only moves known stripes.

All geometry crosses the model boundary in Gemini's native 0-1000 normalized
grid — see app/coords.py — and is converted back to source pixels here.
"""

import json
import os
from typing import Any

from google import genai
from google.genai import types

from app.coords import sniff_image_size, to_normalized, to_pixels
from app.reference import NOTE_BY_INDEX, REFERENCE_CALIBRATION, SEGMENT_BY_INDEX, STRIPE_COUNT
from app.schema import REASONING_MAX_CHARS, RESPONSE_SCHEMA

MODEL = os.environ.get("CALIBRATION_AGENT_MODEL", "gemini-2.5-pro")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "xwalk-keyboards-01")

_REFERENCE_WIDTH = REFERENCE_CALIBRATION["referenceFrame"]["width"]
_REFERENCE_HEIGHT = REFERENCE_CALIBRATION["referenceFrame"]["height"]


def _normalized_reference() -> dict[str, Any]:
    """The reference calibration expressed in the same 0-1000 grid the model
    answers in, so it is comparing like with like."""
    return {
        "coordinateSpace": "normalized 0-1000, origin top-left",
        "leftCrosswalk": to_normalized(REFERENCE_CALIBRATION["leftCrosswalk"], _REFERENCE_WIDTH, _REFERENCE_HEIGHT),
        "rightCrosswalk": to_normalized(REFERENCE_CALIBRATION["rightCrosswalk"], _REFERENCE_WIDTH, _REFERENCE_HEIGHT),
        "stripes": [
            {
                "stripeIndex": stripe["stripeIndex"],
                "segment": stripe["segment"],
                "polygon": to_normalized(stripe["polygon"], _REFERENCE_WIDTH, _REFERENCE_HEIGHT),
            }
            for stripe in REFERENCE_CALIBRATION["stripes"]
        ],
    }


INSTRUCTION = f"""\
You are a camera calibration analyst for a fixed traffic camera (511NY View 5056,
West Street at W. 34 St, Manhattan). The camera slowly drifts — wind, thermal
expansion, and occasional re-aims move it by a few degrees over hours or days.

Your job is to RELOCATE a known calibration onto a new frame. You are not
discovering a crosswalk from scratch.

COORDINATE SPACE — read this first:
All coordinates, both in the reference below and in your answer, are NORMALIZED
to a 0-1000 grid over the frame. x=0 is the left edge, x=1000 the right edge,
y=0 the top edge, y=1000 the bottom edge. Never report source pixels.

THE REFERENCE CALIBRATION (hand-authored, normalized 0-1000):
{json.dumps(_normalized_reference(), separators=(",", ":"))}

The reference defines {STRIPE_COUNT} painted crosswalk stripes. Stripes 1-18 are
the LEFT crosswalk (left of the bollard median), read left to right. Stripes
19-25 are the RIGHT crosswalk (right of the median), read left to right. Each
stripe is one painted white bar, described by a 4-point quadrilateral following
that bar's own perspective.

RULES — these matter more than anything else:

1. Return EXACTLY {STRIPE_COUNT} stripe entries, one per reference stripeIndex,
   in ascending stripeIndex order. Never renumber, reorder, merge, split, or
   drop a stripe. The stripeIndex is an identity, not a position in your output.
2. If a stripe is genuinely not visible — occluded by a vehicle, buried in snow,
   too faded — set "visible": false, return an empty polygon, and give a short
   "reason". Do NOT guess its position, and do NOT shift other stripes to fill
   the gap.
3. Stripe N in your output must be the SAME physical painted bar as stripe N in
   the reference. Anchor on the reference coordinates, then adjust for how the
   view has moved.
4. A stripe partly hidden by a vehicle but whose position you can still infer
   from its neighbours and the road geometry IS visible — report it. Reserve
   "visible": false for bars you genuinely cannot place.
5. The reference's rightmost stripes extend slightly past the frame edge (x near
   or above 1000). That is expected; report geometry as it truly is, clamping
   only at 1000.

ALSO REPORT:
- leftCrosswalk / rightCrosswalk: a 4-point quadrilateral enclosing each
  crosswalk as a whole. Empty array only if that crosswalk is genuinely absent.
- conditions.crosswalkVisible: false only if the crosswalk cannot be located at
  all — a re-aim away from the intersection, whiteout, total obstruction.
- conditions.obstruction: the dominant thing degrading your view, if any.
- conditions.cameraMoved: "none" if the view matches the reference closely,
  "slight" for ordinary drift, "significant" for an apparent re-aim.
- conditions.repaintSuspected: true if the stripes appear repainted, re-striped,
  or the intersection reconfigured — i.e. the reference geometry itself is now
  wrong, not merely displaced.
- confidence: 0.0-1.0, your honest confidence in the returned geometry.
- reasoning: why this geometry and this status, MAXIMUM {REASONING_MAX_CHARS}
  CHARACTERS. Specific and terse, e.g. "View shifted ~8px down vs reference; all
  25 stripes clear, no obstruction." Operators read this.

Prefer admitting uncertainty over producing confident, wrong geometry. A stale
calibration is recoverable; a confidently wrong one silently breaks the
instrument.
"""

_CLIENT: genai.Client | None = None


def _client() -> genai.Client:
    """Cached singleton — a client created per call is closed when it falls out
    of scope, which surfaces as "Cannot send a request, as the client has been
    closed" on the very first request.

    On Cloud Run this authenticates as the runtime service account via ADC. For
    local runs, CALIBRATION_ACCESS_TOKEN lets you supply a token explicitly
    (e.g. `gcloud auth print-access-token`) so a machine whose ADC points at a
    different account does not have to be re-authenticated.
    """
    global _CLIENT
    if _CLIENT is None:
        credentials = None
        token = os.environ.get("CALIBRATION_ACCESS_TOKEN")
        if token:
            from google.oauth2.credentials import Credentials

            credentials = Credentials(token=token)
        _CLIENT = genai.Client(
            vertexai=True, project=PROJECT, location=LOCATION, credentials=credentials
        )
    return _CLIENT


def analyse_frame(image_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
    """Run one calibration pass over a single frame.

    Returns the record with geometry converted to source pixels, plus the raw
    normalized response under `_normalized` for auditing.
    """
    width, height = sniff_image_size(image_bytes)

    response = _client().models.generate_content(
        model=MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    types.Part.from_text(
                        text="Relocate the reference calibration onto this frame. "
                        "Answer in the normalized 0-1000 grid. Follow the stripe identity rules exactly."
                    ),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCTION,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.0,
        ),
    )

    normalized = json.loads(response.text)
    payload: dict[str, Any] = json.loads(json.dumps(normalized))  # deep copy

    for key in ("leftCrosswalk", "rightCrosswalk"):
        polygon = payload.get(key) or []
        payload[key] = to_pixels(polygon, width, height) if polygon else []

    for stripe in payload.get("stripes", []):
        index = stripe.get("stripeIndex")
        # Note and segment always come from the reference, never the model.
        stripe["note"] = NOTE_BY_INDEX.get(index)
        stripe["segment"] = SEGMENT_BY_INDEX.get(index)
        polygon = stripe.get("polygon") or []
        stripe["polygon"] = to_pixels(polygon, width, height) if polygon else []

    reasoning = payload.get("reasoning") or ""
    if len(reasoning) > REASONING_MAX_CHARS:
        payload["reasoning"] = reasoning[: REASONING_MAX_CHARS - 1].rstrip() + "…"

    usage = getattr(response, "usage_metadata", None)
    payload["_usage"] = (
        {
            "promptTokens": getattr(usage, "prompt_token_count", None),
            "responseTokens": getattr(usage, "candidates_token_count", None),
            "thoughtsTokens": getattr(usage, "thoughts_token_count", None),
            "totalTokens": getattr(usage, "total_token_count", None),
        }
        if usage
        else None
    )
    payload["_model"] = MODEL
    payload["_frameSize"] = {"width": width, "height": height}
    payload["_normalized"] = normalized

    return payload
