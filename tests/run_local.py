"""Run one calibration pass locally and print the full record.

    uv run python tests/run_local.py images/videoframe_872991.png [--persist]

Prints everything the agent returns, including every polygon, so the output can
be confirmed by hand before any of it is trusted.
"""

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import MODEL, analyse_frame  # noqa: E402
from app.reference import REFERENCE_CALIBRATION  # noqa: E402
from app.schema import derive_status, validate  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    path = Path(sys.argv[1])
    should_persist = "--persist" in sys.argv
    image = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"

    print(f"frame   : {path} ({len(image):,} bytes)")
    print(f"model   : {MODEL}")
    print("calling ...", flush=True)

    started = time.monotonic()
    payload = analyse_frame(image, mime_type=mime)
    elapsed = time.monotonic() - started

    validation = validate(payload)
    status = derive_status(payload, validation)
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"

    record = {
        "runId": run_id,
        "cameraId": REFERENCE_CALIBRATION["cameraId"],
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
        "elapsedMs": round(elapsed * 1000),
    }

    out_dir = Path(__file__).resolve().parent.parent / "out"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{run_id}.json"
    out_file.write_text(json.dumps(record, indent=2))

    print(f"\n=== FULL RECORD ({elapsed:.1f}s) -> {out_file} ===")
    print(json.dumps(record, indent=2))

    print("\n=== SUMMARY ===")
    print(f"status      : {status}")
    print(f"confidence  : {payload.get('confidence')}")
    print(f"reasoning   : {payload.get('reasoning')!r} ({len(payload.get('reasoning') or '')} chars)")
    print(f"conditions  : {payload.get('conditions')}")
    print(f"gates       : {'PASS' if validation['passed'] else 'FAIL'}")
    for failure in validation["failures"]:
        print(f"  FAIL  {failure}")
    for warning in validation["warnings"]:
        print(f"  warn  {warning}")
    print(f"metrics     : {validation['metrics']}")
    print(f"usage       : {payload.get('_usage')}")

    print("\n=== STRIPE COMPARISON (reference centroid -> returned centroid) ===")
    reference_by_index = {s["stripeIndex"]: s for s in REFERENCE_CALIBRATION["stripes"]}
    for stripe in payload.get("stripes", []):
        index = stripe.get("stripeIndex")
        reference = reference_by_index.get(index)
        note = stripe.get("note")
        segment = stripe.get("segment")
        if not stripe.get("visible") or not stripe.get("polygon"):
            print(f"  {index:>2} {note:<4} {segment:<5} NOT VISIBLE - {stripe.get('reason', '')}")
            continue
        polygon = stripe["polygon"]
        cx = sum(p[0] for p in polygon) / len(polygon)
        cy = sum(p[1] for p in polygon) / len(polygon)
        rx = sum(p[0] for p in reference["polygon"]) / len(reference["polygon"])
        ry = sum(p[1] for p in reference["polygon"]) / len(reference["polygon"])
        delta = ((cx - rx) ** 2 + (cy - ry) ** 2) ** 0.5
        print(
            f"  {index:>2} {note:<4} {segment:<5} "
            f"({rx:6.1f},{ry:6.1f}) -> ({cx:6.1f},{cy:6.1f})  d={delta:5.1f}px"
        )

    if should_persist:
        from app.storage import persist as persist_record

        written = persist_record(record["cameraId"], run_id, record, publish=False)
        print(f"\npersisted: {written}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
