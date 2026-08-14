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

from app.coords import sniff_image_size
from app.tools import analyse_conditions, detect_boundaries, detect_stripes
from app.persist import save

MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_CAMERA_ID = int(os.environ.get("CALIBRATION_CAMERA_ID", "5056"))
API_KEY = os.environ.get("CALIBRATION_AGENT_API_KEY")

app = FastAPI(title="xwalk-camera-calibration-agent")


def _frame_size(image: bytes) -> dict[str, int] | None:
    """Frame dimensions read from the image header.

    Roboflow reports these when detection runs; this covers the case where it
    did not, so the published polygons always carry the frame they were
    measured in.
    """
    try:
        width, height = sniff_image_size(image)
    except Exception:  # noqa: BLE001
        return None
    return {"width": width, "height": height} if width and height else None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"service": "xwalk-camera-calibration-agent", "status": "ok"}


@app.post("/api/calibrate")
async def calibrate(
    request: Request,
    frame: UploadFile = File(...),
    cameraId: int = Form(DEFAULT_CAMERA_ID),
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

    # Step 2: Roboflow detection (if crosswalk is visible). Boundaries run
    # first — they tell the stripe pass which crosswalk each detection belongs
    # to and where to measure slot positions from.
    detection_result: dict[str, Any] | None = None
    boundary_result: dict[str, Any] | None = None
    if status not in ("no_crosswalk", "feed_down"):
        try:
            boundary_result = detect_boundaries(image)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Boundary detection failed: {error}"
            # Non-fatal: stripes are still indexed, off their own extent.

        try:
            detection_result = detect_stripes(image, (boundary_result or {}).get("crosswalks"))
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Stripe detection failed: {error}"
            status = "degraded"

    # Step 3: Determine publishability.
    #
    # Gemini has already rejected frames with no crosswalk, so any stripes
    # Roboflow returns describe real paint. Publish them. Occlusion varies
    # frame to frame and a partial read is still a correct read — each stripe
    # carries its own position, so the client renders what it is given without
    # needing a complete set.
    visible = (detection_result or {}).get("visible_count", 0)
    should_publish = detection_result is not None and visible > 0
    elapsed_ms = round((time.monotonic() - started) * 1000)

    record: dict[str, Any] = {
        "runId": run_id,
        "cameraId": cameraId,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reasoning": reasoning,
        "conditions": conditions,
        "confidence": confidence,
        "referenceFrame": (detection_result or {}).get("referenceFrame") or _frame_size(image),
        "crosswalks": (boundary_result or {}).get("crosswalks"),
        "leftCrosswalk": (boundary_result or {}).get("leftCrosswalk"),
        "rightCrosswalk": (boundary_result or {}).get("rightCrosswalk"),
        "stripes": (detection_result or {}).get("stripes"),
        "stripe_count": (detection_result or {}).get("stripe_count"),
        "visible_count": visible,
        "max_confidence": (detection_result or {}).get("max_confidence"),
        "min_confidence": (detection_result or {}).get("min_confidence"),
        "mean_confidence": (detection_result or {}).get("mean_confidence"),
        "matching_notes": (detection_result or {}).get("segments"),
        "model": model,
        "elapsed_ms": elapsed_ms,
        "gemini_tokens": gemini_tokens,
        "published": should_publish,
    }

    # Step 4: Persist (with the source frame for the archive)
    try:
        record["storage"] = save(record, frame=image)
    except Exception as error:  # noqa: BLE001
        record["storage"] = {"error": str(error)}

    return JSONResponse(record)


SNAPSHOT_URL = os.environ.get(
    "CALIBRATION_SNAPSHOT_URL",
    "https://511ny.org/map/Cctv/5056",
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

    camera_id = DEFAULT_CAMERA_ID
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

    # Step 2: Roboflow detection — boundaries first, same as /api/calibrate.
    detection_result: dict[str, Any] | None = None
    boundary_result: dict[str, Any] | None = None
    if status not in ("no_crosswalk", "feed_down"):
        try:
            boundary_result = detect_boundaries(image)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Boundary detection failed: {error}"

        try:
            detection_result = detect_stripes(image, (boundary_result or {}).get("crosswalks"))
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Stripe detection failed: {error}"
            status = "degraded"

    visible = (detection_result or {}).get("visible_count", 0)
    should_publish = detection_result is not None and visible > 0
    elapsed_ms = round((time.monotonic() - started) * 1000)

    record: dict[str, Any] = {
        "runId": run_id,
        "cameraId": camera_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reasoning": reasoning,
        "conditions": conditions,
        "confidence": confidence,
        "referenceFrame": (detection_result or {}).get("referenceFrame") or _frame_size(image),
        "crosswalks": (boundary_result or {}).get("crosswalks"),
        "leftCrosswalk": (boundary_result or {}).get("leftCrosswalk"),
        "rightCrosswalk": (boundary_result or {}).get("rightCrosswalk"),
        "stripes": (detection_result or {}).get("stripes"),
        "stripe_count": (detection_result or {}).get("stripe_count"),
        "visible_count": visible,
        "max_confidence": (detection_result or {}).get("max_confidence"),
        "min_confidence": (detection_result or {}).get("min_confidence"),
        "mean_confidence": (detection_result or {}).get("mean_confidence"),
        "matching_notes": (detection_result or {}).get("segments"),
        "model": model,
        "elapsed_ms": elapsed_ms,
        "gemini_tokens": gemini_tokens,
        "published": should_publish,
        "source": "scheduled",
    }

    try:
        record["storage"] = save(record, frame=image)
    except Exception as error:  # noqa: BLE001
        record["storage"] = {"error": str(error)}

    return JSONResponse(record)
