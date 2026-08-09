"""Run both variants over the same frame and compare them head to head.

    uv run python tests/compare_variants.py images/videoframe_872991.png

Writes out/compare-<variant>.json plus a stacked overlay so the two can be
judged side by side rather than from metrics alone.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from app.agent import analyse_frame  # noqa: E402
from app.detect import detect_frame  # noqa: E402
from app.match import match  # noqa: E402
from app.reference import REFERENCE_CALIBRATION  # noqa: E402
from app.schema import derive_status, validate  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out"


def summarise(name: str, payload: dict, elapsed: float) -> dict:
    validation = validate(payload)
    status = derive_status(payload, validation)
    metrics = validation["metrics"]
    print(f"\n--- {name} ---")
    print(f"  status      : {status}   confidence {payload.get('confidence')}")
    print(f"  reasoning   : {payload.get('reasoning')}")
    print(f"  gates       : {'PASS' if validation['passed'] else 'FAIL'}")
    for failure in validation["failures"]:
        print(f"    FAIL {failure}")
    print(f"  visible     : {metrics['visibleStripes']}/{metrics['totalStripes']}")
    print(f"  seam gap    : {metrics['maxSeamGapPx']}px")
    print(f"  displacement: max {metrics['maxDisplacementPx']}px  mean {metrics['meanDisplacementPx']}px")
    if payload.get("matching"):
        print(f"  matching    : {payload['matching']['counts']}  clean={payload['matching']['clean']}")
        for note in payload["matching"]["notes"]:
            print(f"    note {note}")
    print(f"  tokens      : {(payload.get('_usage') or {}).get('totalTokens')}   {elapsed:.1f}s")
    return {"status": status, "validation": validation, "payload": payload}


def overlay(frame: Path, results: dict[str, dict], out_path: Path) -> None:
    base = Image.open(frame).convert("RGB")
    scale = 8
    panels = []
    for name, result in results.items():
        img = base.resize((base.width * scale, base.height * scale), Image.LANCZOS)
        draw = ImageDraw.Draw(img, "RGBA")
        for stripe in result["payload"].get("stripes", []):
            polygon = stripe.get("polygon") or []
            if len(polygon) >= 3:
                draw.polygon([(p[0] * scale, p[1] * scale) for p in polygon], outline=(0, 255, 140, 255))
        crop = img.crop((0, 106 * scale, 200 * scale, 156 * scale))
        ImageDraw.Draw(crop).text((10, 10), name, fill=(255, 255, 0))
        panels.append(crop)

    stacked = Image.new("RGB", (panels[0].width, sum(p.height + 8 for p in panels)), (0, 0, 0))
    y = 0
    for panel in panels:
        stacked.paste(panel, (0, y))
        y += panel.height + 8
    stacked.save(out_path)
    print(f"\nwrote {out_path}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    frame = Path(sys.argv[1])
    image = frame.read_bytes()
    mime = "image/png" if frame.suffix.lower() == ".png" else "image/jpeg"
    OUT.mkdir(exist_ok=True)

    print(f"frame: {frame}   reference: {len(REFERENCE_CALIBRATION['stripes'])} stripes")

    results = {}

    started = time.monotonic()
    anchored = analyse_frame(image, mime_type=mime)
    results["ANCHORED (reference supplied, model relocates)"] = summarise(
        "ANCHORED", anchored, time.monotonic() - started
    )

    started = time.monotonic()
    detection = detect_frame(image, mime_type=mime)
    detected_cells = len(detection.get("cells", []))
    matched = match(detection)
    print(f"\n  (cold detection found {detected_cells} cells before matching)")
    results["DETECT-THEN-MATCH (cold detection, code assigns identity)"] = summarise(
        "DETECT-THEN-MATCH", matched, time.monotonic() - started
    )

    (OUT / "compare-anchored.json").write_text(json.dumps(anchored, indent=2))
    (OUT / "compare-detect.json").write_text(json.dumps(matched, indent=2))
    overlay(frame, results, OUT / "compare-variants.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
