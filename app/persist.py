"""Persistence: BigQuery (historical record) + GCS (live calibration).

Every run appends one row to BigQuery. Successful runs also overwrite the
GCS object the web client reads.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

BUCKET = os.environ.get("CALIBRATION_BUCKET", "xwalk-keyboards-01")
BQ_TABLE = os.environ.get("CALIBRATION_BQ_TABLE", "xwalk-keyboards-01.calibration.runs")
GCS_PREFIX = os.environ.get("CALIBRATION_GCS_PREFIX", "calibration")


def _gcs_path(camera_id: int) -> str:
    return f"{GCS_PREFIX}/current/camera_{camera_id}.json"


def save(record: dict[str, Any]) -> dict[str, Any]:
    """Append to BigQuery and optionally publish to GCS. Returns storage paths."""
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
            "expected_count": record.get("expected_count"),
            "count_match": record.get("count_match"),
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

    # --- GCS (auto-publish on ok) ---
    if record.get("published"):
        try:
            from google.cloud import storage

            bucket = storage.Client().bucket(BUCKET)
            path = _gcs_path(record["cameraId"])
            blob = bucket.blob(path)

            calibration = {
                "cameraId": record["cameraId"],
                "updatedAt": record["createdAt"],
                "status": record["status"],
                "reasoning": record["reasoning"],
                "conditions": record["conditions"],
                "confidence": record["confidence"],
                "referenceFrame": record.get("referenceFrame"),
                "stripes": record.get("stripes"),
            }
            blob.upload_from_string(
                json.dumps(calibration, indent=2),
                content_type="application/json",
            )
            result["gcs"] = f"gs://{BUCKET}/{path}"
        except Exception as exc:  # noqa: BLE001
            result["gcs"] = {"error": str(exc)}

    return result
