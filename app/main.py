"""HTTP surface for the calibration agent.

    GET  /health              public health check
    POST /api/calibrate       multipart frame -> calibration record

Phase 0 is analysis-only: a run is archived but never promoted to the live
calibration unless ?publish=true is passed explicitly.
"""

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.agent import MODEL, analyse_frame
from app.reference import REFERENCE_CALIBRATION
from app.schema import derive_status, validate

MAX_IMAGE_BYTES = 8 * 1024 * 1024
API_KEY = os.environ.get("CALIBRATION_AGENT_API_KEY")

app = FastAPI(title="xwalk-camera-calibration-agent")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"service": "xwalk-camera-calibration-agent", "status": "ok", "model": MODEL}


@app.post("/api/calibrate")
async def calibrate(
    request: Request,
    frame: UploadFile = File(...),
    cameraId: int = Form(REFERENCE_CALIBRATION["cameraId"]),
    publish: bool = Form(False),
    persist: bool = Form(True),
) -> JSONResponse:
    if API_KEY and request.headers.get("x-api-key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    image = await frame.read()
    if not image:
        raise HTTPException(status_code=400, detail="Empty frame")
    if len(image) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Frame too large")

    run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    started = time.monotonic()

    try:
        payload = analyse_frame(image, mime_type=frame.content_type or "image/png")
    except Exception as error:  # noqa: BLE001 - surface the cause to the caller
        raise HTTPException(status_code=502, detail=f"Model call failed: {error}") from error

    elapsed_ms = round((time.monotonic() - started) * 1000)
    validation = validate(payload)
    status = derive_status(payload, validation)

    record: dict[str, Any] = {
        "runId": run_id,
        "cameraId": cameraId,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reasoning": payload.get("reasoning"),
        "conditions": payload.get("conditions"),
        "confidence": payload.get("confidence"),
        "referenceFrame": REFERENCE_CALIBRATION["referenceFrame"],
        "leftCrosswalk": payload.get("leftCrosswalk"),
        "rightCrosswalk": payload.get("rightCrosswalk"),
        "stripes": payload.get("stripes"),
        "validation": validation,
        "model": payload.get("_model"),
        "usage": payload.get("_usage"),
        "elapsedMs": elapsed_ms,
        "published": bool(publish and validation["passed"]),
    }

    if persist:
        try:
            from app.storage import persist as persist_record

            record["storage"] = persist_record(
                cameraId, run_id, record, publish=bool(publish and validation["passed"])
            )
        except Exception as error:  # noqa: BLE001 - persistence must not lose the analysis
            record["storage"] = {"error": str(error)}

    return JSONResponse(record)
