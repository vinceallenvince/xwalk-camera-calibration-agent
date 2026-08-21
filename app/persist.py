"""Persistence: BigQuery (historical record) + GCS (live calibration + archive).

Every run:
  - Appends one row to BigQuery (the Looker Studio source).
  - Archives the JSON record AND the source frame to GCS history.
  - When the run published (any visible stripes — see main.run_calibration),
    overwrites the current/ JSON that the web client reads. The image is never
    written to current/ — the client only needs polygons.

GCS layout:
  calibration/
    current/
      camera_5056.json                          ← web client reads this
    history/
      camera_5056/
        run-20260810T135245Z-ea461a.json         ← every run
        run-20260810T135245Z-ea461a.png          ← the frame that was analysed
"""

import json
import os
from typing import Any

BUCKET = os.environ.get("CALIBRATION_BUCKET", "xwalk-keyboards-01")
BQ_TABLE = os.environ.get("CALIBRATION_BQ_TABLE", "xwalk-keyboards-01.calibration.runs")
GCS_PREFIX = os.environ.get("CALIBRATION_GCS_PREFIX", "calibration")


def _current_path(camera_id: int) -> str:
    return f"{GCS_PREFIX}/current/camera_{camera_id}.json"


def _history_path(camera_id: int, run_id: str, ext: str) -> str:
    return f"{GCS_PREFIX}/history/camera_{camera_id}/{run_id}.{ext}"


# The keys the web client reads from current/. This is an allowlist, not a
# copy of the record: everything the client does not need (timings, token
# usage, raw counts) stays in the history archive and BigQuery.
#
# It is deliberately a separate list, so adding a field to the record does not
# silently widen the public payload. The cost is that it can drift the other
# way — VIN-46 removed the boundary polygons from the record but left them
# named here, so every run published `"leftCrosswalk": null` until VIN-49.
# test_persist.py pins the shape against exactly that.
CURRENT_KEYS = (
    ("cameraId", "cameraId"),
    ("updatedAt", "createdAt"),
    ("status", "status"),
    ("reasoning", "reasoning"),
    ("conditions", "conditions"),
    ("confidence", "confidence"),
    ("referenceFrame", "referenceFrame"),
    ("stripes", "stripes"),
)


def current_payload(record: dict[str, Any]) -> dict[str, Any]:
    """The calibration the web client reads, projected from a run record."""
    return {published: record.get(source) for published, source in CURRENT_KEYS}


def save(record: dict[str, Any], frame: bytes | None = None) -> dict[str, Any]:
    """Append to BigQuery, archive to GCS history, and optionally publish.

    `frame` is the raw image bytes used for this calibration run. When provided
    it is archived alongside the JSON record so every run is self-contained.
    """
    result: dict[str, Any] = {}

    # --- BigQuery ---
    try:
        from google.cloud import bigquery

        client = bigquery.Client()
        row = {
            "run_id": record.get("runId"),
            "camera_id": record.get("cameraId"),
            "created_at": record.get("createdAt"),
            "status": record.get("status"),
            "reasoning": record.get("reasoning"),
            "conditions": json.dumps(record.get("conditions")),
            "confidence": record.get("confidence"),
            "stripe_count": record.get("stripe_count"),
            "visible_count": record.get("visible_count"),
            "max_confidence": record.get("max_confidence"),
            "min_confidence": record.get("min_confidence"),
            "mean_confidence": record.get("mean_confidence"),
            "model": record.get("model"),
            "elapsed_ms": record.get("elapsed_ms"),
            "gemini_tokens": record.get("gemini_tokens"),
            "stripes": json.dumps(record.get("stripes")),
            "matching_notes": json.dumps(record.get("matching_notes")),
            "published": record.get("published", False),
        }
        errors = client.insert_rows_json(BQ_TABLE, [row])
        result["bigquery"] = BQ_TABLE if not errors else {"errors": errors}
    except Exception as exc:  # noqa: BLE001
        result["bigquery"] = {"error": str(exc)}

    # --- GCS history (every run) ---
    try:
        from google.cloud import storage

        bucket = storage.Client().bucket(BUCKET)
        run_id = record.get("runId", "unknown")
        camera_id = record.get("cameraId", 5056)

        # Archive the JSON record.
        history_json_path = _history_path(camera_id, run_id, "json")
        bucket.blob(history_json_path).upload_from_string(
            json.dumps(record, indent=2), content_type="application/json",
        )
        result["history_json"] = f"gs://{BUCKET}/{history_json_path}"

        # Archive the source frame alongside it.
        if frame:
            is_png = frame[:4] == b"\x89PNG"
            ext = "png" if is_png else "jpg"
            mime = "image/png" if is_png else "image/jpeg"
            history_frame_path = _history_path(camera_id, run_id, ext)
            bucket.blob(history_frame_path).upload_from_string(frame, content_type=mime)
            result["history_frame"] = f"gs://{BUCKET}/{history_frame_path}"
    except Exception as exc:  # noqa: BLE001
        result["history"] = {"error": str(exc)}

    # --- GCS current (only on successful publish) ---
    if record.get("published"):
        try:
            from google.cloud import storage as gcs

            bucket = gcs.Client().bucket(BUCKET)
            path = _current_path(record["cameraId"])

            bucket.blob(path).upload_from_string(
                json.dumps(current_payload(record), indent=2),
                content_type="application/json",
            )
            result["current"] = f"gs://{BUCKET}/{path}"
        except Exception as exc:  # noqa: BLE001
            result["current"] = {"error": str(exc)}

    return result
