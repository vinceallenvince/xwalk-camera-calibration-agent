"""Draw an archived run's polygons over its source frame for eyeball checks.

    uv run python tests/overlay.py images/videoframe_872991-no-occlusion.png out/<run>.json

Works on the records the agent publishes and archives (the GCS history pairs
of <runId>.json + <runId>.png): crosswalk boundaries are drawn in amber and
stripes in mint, each labelled with its stripeIndex. Coordinates are scaled
from the record's referenceFrame, so a frame grabbed at a different resolution
still lines up. Output is upscaled so a 352 x 240 source is inspectable.
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 4
STRIPE_COLOR = (148, 215, 181)
CROSSWALK_COLOR = (255, 190, 90)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    frame_path = Path(sys.argv[1])
    record = json.loads(Path(sys.argv[2]).read_text())

    base = Image.open(frame_path).convert("RGB")
    canvas = base.resize((base.width * SCALE, base.height * SCALE), Image.NEAREST)
    draw = ImageDraw.Draw(canvas, "RGBA")

    reference = record.get("referenceFrame") or {}
    sx = canvas.width / reference.get("width", base.width)
    sy = canvas.height / reference.get("height", base.height)

    def scaled(polygon):
        return [(p[0] * sx, p[1] * sy) for p in polygon]

    # Crosswalk boundaries: the segment map, falling back to the flat aliases
    # older records carry.
    crosswalks = record.get("crosswalks") or {
        "left": record.get("leftCrosswalk") or [],
        "right": record.get("rightCrosswalk") or [],
    }
    for polygon in crosswalks.values():
        if len(polygon) >= 3:
            draw.polygon(scaled(polygon), outline=(*CROSSWALK_COLOR, 255))

    per_segment: dict[str, list[int]] = {}
    for stripe in record.get("stripes") or []:
        polygon = stripe.get("polygon") or []
        if len(polygon) < 3:
            continue
        points = scaled(polygon)
        draw.polygon(points, fill=(*STRIPE_COLOR, 90), outline=(*STRIPE_COLOR, 255))
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        index = stripe.get("stripeIndex")
        draw.text((cx - 6, cy - 4), str(index), fill=(255, 255, 255))
        if index is not None:
            per_segment.setdefault(stripe.get("segment", "?"), []).append(index)

    out = Path(sys.argv[2]).with_suffix(".overlay.png")
    canvas.save(out)

    print(f"wrote {out}  ({canvas.width} x {canvas.height})")
    print(f"status    : {record.get('status')}  confidence {record.get('confidence')}")
    print(f"reasoning : {record.get('reasoning')}")
    for name in sorted(per_segment):
        indexes = sorted(per_segment[name])
        gaps = sorted(set(range(indexes[0], indexes[-1] + 1)) - set(indexes))
        print(
            f"{name:<9} : {len(indexes)} stripes, "
            f"indexes {indexes[0]}-{indexes[-1]}, hidden {gaps or 'none'}"
        )
    print("legend    : mint = stripes (stripeIndex labelled), amber = crosswalk boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
