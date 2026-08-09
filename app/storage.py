"""Persistence for calibration runs.

Phase 0 writes to GCS: a `current/` object per camera plus an append-only
`history/` object per run. GCS object versioning gives audit and rollback for
free, which is why this is the recommended store over Cloud SQL for what is a
single small JSON document per camera.
"""

import json
import os
from typing import Any

BUCKET = os.environ.get("CALIBRATION_BUCKET", "xwalk-keyboards-01")
PREFIX = os.environ.get("CALIBRATION_PREFIX", "calibration")


def _bucket():
    from google.cloud import storage

    return storage.Client().bucket(BUCKET)


def current_path(camera_id: int) -> str:
    return f"{PREFIX}/current/camera_{camera_id}.json"


def history_path(camera_id: int, run_id: str) -> str:
    return f"{PREFIX}/history/camera_{camera_id}/{run_id}.json"


def persist(camera_id: int, run_id: str, record: dict[str, Any], *, publish: bool) -> dict[str, str]:
    """Always archive the run; only overwrite `current` when publish is True.

    Phase 0 never publishes — every run is archived so the evaluation set builds
    itself, and the live calibration is left alone.
    """
    bucket = _bucket()
    body = json.dumps(record, indent=2, sort_keys=True)

    written: dict[str, str] = {}

    history = bucket.blob(history_path(camera_id, run_id))
    history.upload_from_string(body, content_type="application/json")
    written["history"] = f"gs://{BUCKET}/{history.name}"

    if publish:
        current = bucket.blob(current_path(camera_id))
        current.upload_from_string(body, content_type="application/json")
        written["current"] = f"gs://{BUCKET}/{current.name}"

    return written


def read_current(camera_id: int) -> dict[str, Any] | None:
    blob = _bucket().blob(current_path(camera_id))
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())
