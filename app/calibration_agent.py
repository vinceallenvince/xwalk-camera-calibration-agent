"""The ADK calibration agent.

An ADK agent with two tools. On each run it:
  1. Calls analyse_conditions (Gemini Flash) to classify the frame.
  2. If the crosswalk is visible, calls detect_stripes (Roboflow) for geometry.
  3. Assembles the record and persists to BigQuery + GCS.

The agent decides whether to proceed based on the conditions result — the
orchestration is in its reasoning loop, not hardcoded.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from google.adk import Agent
from google.adk.tools import FunctionTool

from app.reference import REFERENCE_CALIBRATION, STRIPE_COUNT
from app.tools import analyse_conditions, detect_stripes

MODEL = os.environ.get("CALIBRATION_AGENT_MODEL", "gemini-2.5-flash")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "xwalk-keyboards-01")

# The image bytes are injected into the session state before the agent runs.
# Tools read from this key rather than accepting bytes as a parameter (ADK
# tool parameters are serialised as JSON, and passing ~200KB of base64 through
# the reasoning loop wastes tokens and risks truncation).
_IMAGE_KEY = "current_frame"
_MIME_KEY = "current_frame_mime"


def _tool_analyse_conditions(tool_context) -> dict[str, Any]:
    """Gemini Flash: classify the frame's condition."""
    state = tool_context.state
    image = state.get(_IMAGE_KEY)
    mime = state.get(_MIME_KEY, "image/png")
    if not image:
        return {"error": "No frame available in session state"}
    return analyse_conditions(image, mime_type=mime)


def _tool_detect_stripes(tool_context) -> dict[str, Any]:
    """Roboflow: detect and match stripe polygons."""
    state = tool_context.state
    image = state.get(_IMAGE_KEY)
    if not image:
        return {"error": "No frame available in session state"}
    return detect_stripes(image)


calibration_agent = Agent(
    model=MODEL,
    name="calibration_agent",
    description="Analyses a traffic camera frame and produces crosswalk calibration data.",
    instruction="""\
You are the XWALK KEYBOARDS calibration agent. You maintain the crosswalk
stripe calibration for a live traffic camera (View 5056, West Street at
W. 34 St, Manhattan).

On each run you receive a current camera frame in session state. Your job:

1. FIRST, call `analyse_conditions` to classify the frame. This tells you
   whether the crosswalk is visible and what conditions are present.

2. Look at the status returned:
   - If status is "no_crosswalk" or "feed_down": STOP. Return the conditions
     result as your final answer. There is nothing to detect.
   - If status is "ok", "degraded", or "needs_review": PROCEED to step 3.

3. Call `detect_stripes` to get the stripe polygons from Roboflow.

4. Return a JSON object combining both results:
   {
     "status": <from conditions>,
     "reasoning": <from conditions>,
     "conditions": <from conditions>,
     "confidence": <from conditions>,
     "stripes": <from detection>,
     "stripe_count": <from detection>,
     "visible_count": <from detection>,
     "expected_count": <from detection>,
     "count_match": <from detection>,
     "matching": <from detection>,
     "detection_confidence": {
       "max": <from detection>,
       "min": <from detection>,
       "mean": <from detection>
     }
   }

   If detection shows a count mismatch (count_match is false), change the
   status to "needs_review" regardless of what conditions said.

Always return valid JSON as your final answer. Do not add commentary outside
the JSON.
""",
    tools=[
        FunctionTool(func=_tool_analyse_conditions),
        FunctionTool(func=_tool_detect_stripes),
    ],
)
