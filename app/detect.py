"""Detect-then-match variant.

The anchored variant (app/agent.py) hands the model the reference coordinates
and asks it to relocate them. That biases it toward transforming stale geometry
rather than looking at the image — one run described its own method as
"relocated via linear shift".

This variant splits the two jobs:

  1. DETECT — the model finds crosswalk cells with no reference coordinates at
     all. Pure vision, which is what models are good at.
  2. MATCH  — code assigns reference stripeIndex and note to the detected cells
     deterministically (app/match.py). Pure bookkeeping, which code should own.

A count mismatch then becomes an explicit, visible discrepancy instead of a
silent renumbering.
"""

import json
import os
from typing import Any

from google.genai import types

from app.coords import sniff_image_size, to_pixels
from app.schema import REASONING_MAX_CHARS

DETECT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": ["leftCrosswalk", "rightCrosswalk", "cells", "conditions", "confidence", "reasoning"],
    "properties": {
        "leftCrosswalk": {
            "type": "ARRAY",
            "description": "4 [x,y] points enclosing the left crosswalk, or empty if absent.",
            "items": {"type": "ARRAY", "items": {"type": "NUMBER"}},
        },
        "rightCrosswalk": {
            "type": "ARRAY",
            "description": "4 [x,y] points enclosing the right crosswalk, or empty if absent.",
            "items": {"type": "ARRAY", "items": {"type": "NUMBER"}},
        },
        "cells": {
            "type": "ARRAY",
            "description": "Every cell found, ordered left to right within each segment, left segment first.",
            "items": {
                "type": "OBJECT",
                "required": ["order", "segment", "polygon"],
                "properties": {
                    "order": {"type": "INTEGER", "description": "0-based position within its own segment."},
                    "segment": {"type": "STRING", "enum": ["left", "right"]},
                    "polygon": {
                        "type": "ARRAY",
                        "description": "4 [x,y] points: top-left, top-right, bottom-right, bottom-left.",
                        "items": {"type": "ARRAY", "items": {"type": "NUMBER"}},
                    },
                    "occluded": {
                        "type": "BOOLEAN",
                        "description": "True when the bar is hidden but its position was inferred from neighbours.",
                    },
                },
            },
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

INSTRUCTION = f"""\
We are building a playable keyboard from the crosswalks in a fixed traffic
camera frame (511NY View 5056, West Street at W. 34 St, Manhattan). Each painted
white bar becomes one key, so we need the bars located as CELLS.

There are two crosswalks in this view, separated by a bollard median: a LEFT
crosswalk and a RIGHT crosswalk. Report each separately.

COORDINATE SPACE:
All coordinates are NORMALIZED to a 0-1000 grid over the frame. x=0 is the left
edge, x=1000 the right edge, y=0 the top edge, y=1000 the bottom edge. Never
report source pixels. Values may exceed 1000 where geometry genuinely runs off
the frame edge — do not clamp, because clamping collapses the last cell into a
degenerate sliver.

CELL GEOMETRY — a playable keyboard, not an outline of the paint:

Each cell is CENTRED on one painted white bar and extends outward to the MIDDLE
of the dark gap on each side. Do NOT align a cell edge to the edge of a bar.

    paint:      ██  gap  ██  gap  ██
    cells:    |--A--|--B--|--C--|      <- seams sit mid-gap, cells touch

- The seam between consecutive cells runs down the MIDDLE of the dark gap
  between their two bars.
- Cells within a crosswalk TILE CONTIGUOUSLY: cell N's top-right point equals
  cell N+1's top-left point, and cell N's bottom-right point equals cell N+1's
  bottom-left point. No gaps, no overlaps.
- The outer edge of the first and last cell extends half a gap beyond the
  outermost bar, so coverage is symmetric.
- Each polygon is [top-left, top-right, bottom-right, bottom-left], following
  that bar's own perspective — the bars lean, so the bottom edge is offset from
  the top edge. The cell should span the bar's full painted length.

This matters because a pedestrian standing between two bars must still play a
note — the nearest one. Cells that only cover paint leave dead zones.

RULES:
1. Report EVERY bar you can place, ordered left to right within each segment,
   with `order` counting from 0 within that segment. Left segment first.
2. A bar partly hidden by a vehicle whose position you can still infer from its
   neighbours and the road geometry IS reportable — include it and set
   `occluded: true`. Only omit bars you genuinely cannot place.
3. Do not invent bars to reach a round number, and do not merge two bars into
   one cell. Report what is actually painted.
4. Count carefully. The number of cells is used to detect changes to the
   crossing, so an accurate count matters as much as accurate geometry.

ALSO REPORT:
- leftCrosswalk / rightCrosswalk: a 4-point quadrilateral enclosing each
  crosswalk as a whole, following the same perspective as its cells.
- conditions.crosswalkVisible: false only if a crosswalk cannot be located at
  all — a re-aim away from the intersection, whiteout, total obstruction.
- conditions.obstruction: the dominant thing degrading your view, if any.
- conditions.cameraMoved: always "none" here; you have no reference to compare
  against. Drift is measured downstream.
- conditions.repaintSuspected: true if the paint looks freshly re-striped or the
  crossing appears reconfigured.
- confidence: 0.0-1.0, your honest confidence in the returned geometry.
- reasoning: why this geometry and this status, MAXIMUM {REASONING_MAX_CHARS}
  CHARACTERS. Specific and terse. Operators read this.

Prefer admitting uncertainty over producing confident, wrong geometry.
"""


def detect_frame(image_bytes: bytes, mime_type: str = "image/png") -> dict[str, Any]:
    """Cold detection — no reference coordinates are supplied to the model."""
    from app.agent import MODEL, _client, _upscaled

    width, height = sniff_image_size(image_bytes)
    sent_bytes, sent_mime = _upscaled(image_bytes, mime_type)

    response = _client().models.generate_content(
        model=MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=sent_bytes, mime_type=sent_mime),
                    types.Part.from_text(
                        text="Detect the crosswalk cells in this frame. "
                        "Answer in the normalized 0-1000 grid. Follow the cell geometry rules exactly."
                    ),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCTION,
            response_mime_type="application/json",
            response_schema=DETECT_SCHEMA,
            temperature=0.0,
        ),
    )

    normalized = json.loads(response.text)
    payload: dict[str, Any] = json.loads(json.dumps(normalized))

    for key in ("leftCrosswalk", "rightCrosswalk"):
        polygon = payload.get(key) or []
        payload[key] = to_pixels(polygon, width, height) if polygon else []

    for cell in payload.get("cells", []):
        polygon = cell.get("polygon") or []
        cell["polygon"] = to_pixels(polygon, width, height) if polygon else []

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
