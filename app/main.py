"""HTTP surface for the calibration agent.

    GET  /health                   public health check
    POST /api/calibrate            multipart frame -> full calibration record
    POST /api/calibrate-scheduled  no body needed — fetches a frame from the
                                   camera's snapshot source, then calibrates.
                                   Takes ?cameraId=NNNN; defaults to
                                   CALIBRATION_CAMERA_ID.

Cloud Scheduler hits /api/calibrate-scheduled on a fixed cadence (see the
Cloud Scheduler job for the current interval) — one job per camera, each
addressing its camera via the cameraId query parameter. Manual runs use
/api/calibrate with an explicit frame.
"""

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.cameras import camera_config
from app.coords import sniff_image_size
from app.persist import save
from app.tools import analyse_conditions, detect_stripes

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


def run_calibration(
    image: bytes,
    mime: str,
    camera_id: int,
    source: str | None = None,
) -> dict[str, Any]:
    """The full calibration pipeline, shared by both endpoints.

    Raises whatever analyse_conditions raises — the endpoints translate that
    into an HTTP error. Detection failures degrade the run instead.
    """
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()
    camera = camera_config(camera_id)

    # Step 1: Gemini triage, judged against this camera's registered scene
    conditions_result = analyse_conditions(image, camera, mime_type=mime)

    status = conditions_result.get("status", "degraded")
    reasoning = conditions_result.get("reasoning")
    conditions = conditions_result.get("conditions")
    confidence = conditions_result.get("confidence")
    gemini_tokens = (conditions_result.get("_usage") or {}).get("totalTokens")
    model = conditions_result.get("_model")

    # Step 2: Roboflow stripe detection (if a crosswalk is visible).
    # Segments come from the stripes themselves — gap-clustered along the
    # crosswalk axis, named positionally (see geometry.place_stripes). There
    # is no boundary pass and no run-over-run memory: identities are allowed
    # to wobble, and the client anchors notes from a per-camera base so a
    # renumbering transposes the scale rather than corrupting it (VIN-44).
    detection_result: dict[str, Any] | None = None
    if status not in ("no_crosswalk", "feed_down"):
        try:
            detection_result = detect_stripes(image)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Stripe detection failed: {error}"
            status = "degraded"

    # Step 3: Determine publishability.
    #
    # Gemini has already rejected frames with no crosswalk, so any stripes
    # Roboflow returns describe real paint. Occlusion varies frame to frame
    # and a partial read is still a correct read — each stripe carries its own
    # position, so the client renders what it is given without needing a
    # complete set. Any run that saw a stripe publishes.
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
    if source:
        record["source"] = source

    # Step 4: Persist (with the source frame for the archive)
    try:
        record["storage"] = save(record, frame=image)
    except Exception as error:  # noqa: BLE001
        record["storage"] = {"error": str(error)}

    return record


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

    try:
        record = run_calibration(image, mime, cameraId)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Conditions analysis failed: {error}") from error

    return JSONResponse(record)


# Legacy single-camera override, honored for the default camera only. New
# cameras get their snapshot URL from app/cameras.py (registry or template).
SNAPSHOT_URL_OVERRIDE = os.environ.get("CALIBRATION_SNAPSHOT_URL")


def snapshot_url_for(camera_id: int) -> str:
    if SNAPSHOT_URL_OVERRIDE and camera_id == DEFAULT_CAMERA_ID:
        return SNAPSHOT_URL_OVERRIDE
    return camera_config(camera_id).frame_url


@app.post("/api/calibrate-scheduled")
async def calibrate_scheduled(request: Request, cameraId: int | None = None) -> JSONResponse:
    """Scheduled entry point — fetches the current frame from the camera's
    snapshot source, then runs the same calibration pipeline as /api/calibrate.

    Cloud Scheduler calls this with no body, one job per camera, addressing
    the camera with ?cameraId=NNNN. Omitting it calibrates the default camera
    (CALIBRATION_CAMERA_ID), which keeps the original single-camera job valid.
    """
    if API_KEY and request.headers.get("x-api-key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    camera_id = cameraId if cameraId is not None else DEFAULT_CAMERA_ID

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(snapshot_url_for(camera_id))
            resp.raise_for_status()
            image = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg")
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to fetch camera frame: {error}") from error

    if not image or len(image) < 1000:
        raise HTTPException(status_code=502, detail="Camera frame is empty or too small")

    mime = "image/png" if "png" in content_type else "image/jpeg"

    try:
        record = run_calibration(image, mime, camera_id, source="scheduled")
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Conditions analysis failed: {error}") from error

    return JSONResponse(record)
