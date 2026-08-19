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
from app.continuity import reconcile_segments
from app.coords import sniff_image_size
from app.persist import load_current, save
from app.tools import analyse_conditions, detect_boundaries, detect_stripes

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


def _previous_crosswalks(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Boundary polygons from the last published calibration, keyed by segment.

    The current/ schema publishes flattened left/right aliases; newer records
    may also carry the full `crosswalks` map. Prefer the map so segments
    beyond left/right keep their identity too.
    """
    if not record:
        return None
    crosswalks = record.get("crosswalks")
    if isinstance(crosswalks, dict) and crosswalks:
        return crosswalks

    flattened = {
        name: record.get(f"{name}Crosswalk")
        for name in ("left", "right")
    }
    usable = {name: poly for name, poly in flattened.items() if poly}
    return usable or None


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

    # Step 2: Roboflow detection (if crosswalk is visible). Boundaries run
    # first — they tell the stripe pass which crosswalk each detection belongs
    # to and where to measure slot positions from — and are then reconciled
    # against the last published calibration so segment names keep meaning the
    # same physical crosswalk from run to run. The web client maps names to
    # fixed pitch anchors; a name that drifted would transpose a keyboard.
    detection_result: dict[str, Any] | None = None
    boundary_result: dict[str, Any] | None = None
    crosswalks: dict[str, Any] = {}
    continuity: dict[str, Any] | None = None
    if status not in ("no_crosswalk", "feed_down"):
        previous = load_current(camera_id)

        try:
            boundary_result = detect_boundaries(
                image,
                max_crosswalks=camera.expected_crosswalks,
                min_confidence=camera.boundary_min_confidence,
            )
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Boundary detection failed: {error}"
            # Non-fatal here: continuity decides below whether the run can
            # still publish without boundaries.

        detected = (boundary_result or {}).get("crosswalks") or {}
        crosswalks, continuity = reconcile_segments(
            detected, _previous_crosswalks(previous), camera.expected_crosswalks,
        )

        if continuity["retired"]:
            # The published baseline carried more segments than the camera has
            # crosswalks — phantoms from an occlusion-split, published before
            # the detection cap. They are dropped from the baseline rather
            # than counted as missing, or they would hold publishes forever.
            names = ", ".join(continuity["retired"])
            reasoning = (reasoning or "") + (
                f" Boundary continuity: retired phantom {names} — the registered"
                f" crosswalk count is {camera.expected_crosswalks}."
            )
        if continuity["renamed"]:
            renames = ", ".join(f"{old}->{new}" for old, new in continuity["renamed"].items())
            reasoning = (reasoning or "") + f" Segment continuity: renamed {renames} to match the published calibration."
        if continuity["regression"]:
            # A crosswalk the client is playing was not found this run. Do not
            # overwrite the published calibration with a renamed or merged
            # read — mark the run degraded and hold the last good publish.
            if status == "ok":
                status = "degraded"
            missing = ", ".join(continuity["missing"])
            reasoning = (reasoning or "") + (
                f" Boundary continuity: {missing} not detected this run; holding the published calibration."
            )

        try:
            detection_result = detect_stripes(image, crosswalks or None)
        except Exception as error:  # noqa: BLE001
            reasoning = (reasoning or "") + f" Stripe detection failed: {error}"
            status = "degraded"

    # Step 3: Determine publishability.
    #
    # Gemini has already rejected frames with no crosswalk, so any stripes
    # Roboflow returns describe real paint. Occlusion varies frame to frame
    # and a partial read is still a correct read — each stripe carries its own
    # position, so the client renders what it is given without needing a
    # complete set. The one exception is a boundary continuity regression:
    # publishing a run that lost or merged a previously published crosswalk
    # would rename segments under the client, so those runs are archived but
    # never promoted to current/.
    visible = (detection_result or {}).get("visible_count", 0)
    regression = bool(continuity and continuity["regression"])
    should_publish = detection_result is not None and visible > 0 and not regression
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
        "crosswalks": crosswalks or None,
        "leftCrosswalk": crosswalks.get("left", []),
        "rightCrosswalk": crosswalks.get("right", []),
        "stripes": (detection_result or {}).get("stripes"),
        "stripe_count": (detection_result or {}).get("stripe_count"),
        "visible_count": visible,
        "max_confidence": (detection_result or {}).get("max_confidence"),
        "min_confidence": (detection_result or {}).get("min_confidence"),
        "mean_confidence": (detection_result or {}).get("mean_confidence"),
        "matching_notes": (detection_result or {}).get("segments"),
        "continuity": continuity,
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
