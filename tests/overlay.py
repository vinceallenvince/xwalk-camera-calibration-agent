"""Draw a run's polygons over the source frame so they can be confirmed by eye.

    uv run python tests/overlay.py images/videoframe_872991.png out/<run>.json

Reference geometry is drawn in dim blue, the model's answer in mint, and any
stripe reported not-visible is listed in the caption. Output is upscaled so the
352 x 240 source is actually inspectable.
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.reference import REFERENCE_CALIBRATION  # noqa: E402

SCALE = 4
REFERENCE_COLOR = (90, 140, 255)
RETURNED_COLOR = (148, 215, 181)
CROSSWALK_COLOR = (255, 190, 90)


def _scaled(polygon):
    return [(p[0] * SCALE, p[1] * SCALE) for p in polygon]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    frame_path = Path(sys.argv[1])
    record = json.loads(Path(sys.argv[2]).read_text())

    base = Image.open(frame_path).convert("RGB")
    canvas = base.resize((base.width * SCALE, base.height * SCALE), Image.NEAREST)
    draw = ImageDraw.Draw(canvas, "RGBA")

    # Reference stripes, dim, for comparison.
    for stripe in REFERENCE_CALIBRATION["stripes"]:
        draw.polygon(_scaled(stripe["polygon"]), outline=(*REFERENCE_COLOR, 200))

    # Returned crosswalk quads.
    for key in ("leftCrosswalk", "rightCrosswalk"):
        polygon = record.get(key) or []
        if len(polygon) >= 3:
            draw.polygon(_scaled(polygon), outline=(*CROSSWALK_COLOR, 255))

    # Returned stripes, filled so occupancy is obvious.
    invisible = []
    for stripe in record.get("stripes", []):
        polygon = stripe.get("polygon") or []
        if not stripe.get("visible") or len(polygon) < 3:
            invisible.append(f"{stripe.get('stripeIndex')} {stripe.get('note')}")
            continue
        points = _scaled(polygon)
        draw.polygon(points, fill=(*RETURNED_COLOR, 90), outline=(*RETURNED_COLOR, 255))
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        draw.text((cx - 6, cy - 4), str(stripe.get("stripeIndex")), fill=(255, 255, 255))

    out = Path(sys.argv[2]).with_suffix(".overlay.png")
    canvas.save(out)

    print(f"wrote {out}  ({canvas.width} x {canvas.height})")
    print(f"status     : {record.get('status')}  confidence {record.get('confidence')}")
    print(f"reasoning  : {record.get('reasoning')}")
    print(f"metrics    : {record.get('validation', {}).get('metrics')}")
    print(f"not visible: {invisible or 'none'}")
    print("legend     : blue = reference stripes, mint = returned stripes, amber = returned crosswalk quads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
