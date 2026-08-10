"""HTTP surface for the calibration agent.

    GET  /health                   public health check
    POST /api/calibrate            multipart frame -> full calibration record
    POST /api/calibrate-scheduled  no body needed — fetches a frame from the
                                   web app's snapshot proxy, then runs calibrate

Cloud Scheduler hits /api/calibrate-scheduled every 15 minutes. Manual runs
use /api/calibrate with an explicit frame.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.reference import REFERENCE_CALIBRATION, STRIPE_COUNT
from app.tools import analyse_conditions, detect_boundaries, detect_stripes
from app.persist import save

MAX_IMAGE_BYTES = 8 * 1024 * 1024
API_KEY = os.environ.get("CALIBRATION_AGENT_API_KEY")

app = FastAPI(title="xwalk-camera-calibration-agent")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"service": "xwalk-camera-calibration-agent", "status": "ok"}


@app.post("/api/calibrate")
async def calibrate(
    request: Request,
    frame: UploadFile = File(...),
    cameraId: int = Form(REFERENCE_CALIBRATION["cameraId"]),
) -> JSONResponse:
    if API_KEY and request.headers.get("x-api-key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    image = await frame.read()
    if not image:
        raise HTTPException(status_code=400, detail="Empty frame")
    if len(image) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Frame too large")

    mime = frame.content_type or "image/png"
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()

    # Step 1: Gemini triage
    try:
        conditions_result = analyse_conditions(image, mime_type=mime)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Conditions analysis failed: {error}") from error

    status = conditions_result.get("status", "degraded")
    reasoning = conditions_result.get("reasoning")
    conditions = conditions_result.get("conditions")
    confidence = conditions_result.get("confidence")
    gemini_tokens = (conditions_result.get("_usage") or {}).get("totalTokens")
    model = conditions_result.get("_model")

    # Step 2: Roboflow detection (if crosswalk is visible)
    detection_result: dict[str, Any] | None = None
    boundary_result: dict[str, Any] | None = None
    if status not in ("no_crosswalk", "feed_down"):
        try:
            detection_result = detect_stripes(image)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Stripe detection failed: {error}"
            status = "degraded"

        try:
            boundary_result = detect_boundaries(image)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Boundary detection failed: {error}"
            # Boundary failure is non-fatal — stripes still work, the client
            # falls back to the baked-in crosswalk polygons.

    # Step 3: Merge and determine publishability
    if detection_result and not detection_result.get("count_match"):
        status = "needs_review"

    should_publish = status == "ok" and detection_result is not None
    elapsed_ms = round((time.monotonic() - started) * 1000)

    record: dict[str, Any] = {
        "runId": run_id,
        "cameraId": cameraId,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reasoning": reasoning,
        "conditions": conditions,
        "confidence": confidence,
        "referenceFrame": (detection_result or {}).get("referenceFrame", REFERENCE_CALIBRATION["referenceFrame"]),
        "leftCrosswalk": (boundary_result or {}).get("leftCrosswalk"),
        "rightCrosswalk": (boundary_result or {}).get("rightCrosswalk"),
        "stripes": (detection_result or {}).get("stripes"),
        "stripe_count": (detection_result or {}).get("stripe_count"),
        "visible_count": (detection_result or {}).get("visible_count"),
        "expected_count": STRIPE_COUNT,
        "count_match": (detection_result or {}).get("count_match"),
        "max_confidence": (detection_result or {}).get("max_confidence"),
        "min_confidence": (detection_result or {}).get("min_confidence"),
        "mean_confidence": (detection_result or {}).get("mean_confidence"),
        "matching_notes": (detection_result or {}).get("matching", {}).get("notes"),
        "model": model,
        "elapsed_ms": elapsed_ms,
        "gemini_tokens": gemini_tokens,
        "published": should_publish,
    }

    # Step 4: Persist
    try:
        record["storage"] = save(record)
    except Exception as error:  # noqa: BLE001
        record["storage"] = {"error": str(error)}

    return JSONResponse(record)


SNAPSHOT_URL = os.environ.get(
    "CALIBRATION_SNAPSHOT_URL",
    "https://xwalk-keyboards-21826886868.us-central1.run.app/api/snapshot/5056",
)


@app.post("/api/calibrate-scheduled")
async def calibrate_scheduled(request: Request) -> JSONResponse:
    """Scheduled entry point — fetches a frame from the web app's snapshot proxy,
    then runs the same calibration pipeline as /api/calibrate.

    Cloud Scheduler calls this with no body. The snapshot proxy returns the
    current 511NY camera image for View 5056.
    """
    if API_KEY and request.headers.get("x-api-key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(SNAPSHOT_URL)
            resp.raise_for_status()
            image = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg")
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to fetch camera frame: {error}") from error

    if not image or len(image) < 1000:
        raise HTTPException(status_code=502, detail="Camera frame is empty or too small")

    camera_id = REFERENCE_CALIBRATION["cameraId"]
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    mime = "image/png" if "png" in content_type else "image/jpeg"

    # Step 1: Gemini triage
    try:
        conditions_result = analyse_conditions(image, mime_type=mime)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Conditions analysis failed: {error}") from error

    status = conditions_result.get("status", "degraded")
    reasoning = conditions_result.get("reasoning")
    conditions = conditions_result.get("conditions")
    confidence = conditions_result.get("confidence")
    gemini_tokens = (conditions_result.get("_usage") or {}).get("totalTokens")
    model = conditions_result.get("_model")

    # Step 2: Roboflow detection
    detection_result: dict[str, Any] | None = None
    boundary_result: dict[str, Any] | None = None
    if status not in ("no_crosswalk", "feed_down"):
        try:
            detection_result = detect_stripes(image)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Stripe detection failed: {error}"
            status = "degraded"

        try:
            boundary_result = detect_boundaries(image)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Boundary detection failed: {error}"

    if detection_result and not detection_result.get("count_match"):
        status = "needs_review"

    should_publish = status == "ok" and detection_result is not None
    elapsed_ms = round((time.monotonic() - started) * 1000)

    record: dict[str, Any] = {
        "runId": run_id,
        "cameraId": camera_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reasoning": reasoning,
        "conditions": conditions,
        "confidence": confidence,
        "referenceFrame": (detection_result or {}).get("referenceFrame", REFERENCE_CALIBRATION["referenceFrame"]),
        "leftCrosswalk": (boundary_result or {}).get("leftCrosswalk"),
        "rightCrosswalk": (boundary_result or {}).get("rightCrosswalk"),
        "stripes": (detection_result or {}).get("stripes"),
        "stripe_count": (detection_result or {}).get("stripe_count"),
        "visible_count": (detection_result or {}).get("visible_count"),
        "expected_count": STRIPE_COUNT,
        "count_match": (detection_result or {}).get("count_match"),
        "max_confidence": (detection_result or {}).get("max_confidence"),
        "min_confidence": (detection_result or {}).get("min_confidence"),
        "mean_confidence": (detection_result or {}).get("mean_confidence"),
        "matching_notes": (detection_result or {}).get("matching", {}).get("notes"),
        "model": model,
        "elapsed_ms": elapsed_ms,
        "gemini_tokens": gemini_tokens,
        "published": should_publish,
        "source": "scheduled",
    }

    try:
        record["storage"] = save(record)
    except Exception as error:  # noqa: BLE001
        record["storage"] = {"error": str(error)}

    return JSONResponse(record)
