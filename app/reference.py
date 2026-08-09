"""The hand-authored View 5056 calibration.

This is the anchor for every run. It owns the *musical contract* — how many
stripes exist, their order, their segment, and which note each one plays. The
model never invents those; it only relocates known stripes by index.

Kept in sync with `src/lib/realtime-calibration.ts` in the xwalk-keyboards web
app. Coordinates are in the 352 x 240 native HLS frame of View 5056.
"""

from typing import Any

REFERENCE_CALIBRATION: dict[str, Any] = {
    "cameraId": 5056,
    "referenceFrame": {"width": 352, "height": 240},
    "leftCrosswalk": [[27, 108], [193, 120], [188, 139], [1, 125]],
    "rightCrosswalk": [[291, 132], [349, 137], [350, 155], [301, 150]],
    "stripes": [
        {"stripeIndex": 1, "segment": "left", "note": "C4", "polygon": [[6.5, 118], [15.5, 119], [1, 128], [0, 124]]},
        {"stripeIndex": 2, "segment": "left", "note": "C#4", "polygon": [[15.5, 119], [25, 119.5], [4.5, 132.5], [1, 128]]},
        {"stripeIndex": 3, "segment": "left", "note": "D4", "polygon": [[25, 119.5], [34, 120.5], [14.5, 133.5], [4.5, 132.5]]},
        {"stripeIndex": 4, "segment": "left", "note": "Eb4", "polygon": [[34, 120.5], [43, 121], [25, 134], [14.5, 133.5]]},
        {"stripeIndex": 5, "segment": "left", "note": "E4", "polygon": [[43, 121], [53, 121.5], [34.5, 135], [25, 134]]},
        {"stripeIndex": 6, "segment": "left", "note": "F4", "polygon": [[53, 121.5], [62, 122], [44.5, 136], [34.5, 135]]},
        {"stripeIndex": 7, "segment": "left", "note": "F#4", "polygon": [[62, 122], [71.5, 122.5], [55.5, 136.5], [44.5, 136]]},
        {"stripeIndex": 8, "segment": "left", "note": "G4", "polygon": [[71.5, 122.5], [81, 123], [66, 137], [55.5, 136.5]]},
        {"stripeIndex": 9, "segment": "left", "note": "Ab4", "polygon": [[81, 123], [91, 123.5], [76.5, 138], [66, 137]]},
        {"stripeIndex": 10, "segment": "left", "note": "A4", "polygon": [[91, 123.5], [100, 124.5], [87.5, 138.5], [76.5, 138]]},
        {"stripeIndex": 11, "segment": "left", "note": "Bb4", "polygon": [[100, 124.5], [110, 125], [99, 139], [87.5, 138.5]]},
        {"stripeIndex": 12, "segment": "left", "note": "B4", "polygon": [[110, 125], [120, 125.5], [110, 140], [99, 139]]},
        {"stripeIndex": 13, "segment": "left", "note": "C5", "polygon": [[120, 125.5], [129.5, 126], [121, 141], [110, 140]]},
        {"stripeIndex": 14, "segment": "left", "note": "C#5", "polygon": [[129.5, 126], [139.5, 126.5], [132, 141.5], [121, 141]]},
        {"stripeIndex": 15, "segment": "left", "note": "D5", "polygon": [[139.5, 126.5], [149, 127], [143, 142], [132, 141.5]]},
        {"stripeIndex": 16, "segment": "left", "note": "Eb5", "polygon": [[149, 127], [159, 127.5], [154, 143], [143, 142]]},
        {"stripeIndex": 17, "segment": "left", "note": "E5", "polygon": [[159, 127.5], [169, 128.5], [165, 144.5], [154, 143]]},
        {"stripeIndex": 18, "segment": "left", "note": "F5", "polygon": [[169, 128.5], [178, 129.5], [176, 146.5], [165, 144.5]]},
        {"stripeIndex": 19, "segment": "right", "note": "F#5", "polygon": [[278, 137.5], [287, 138.5], [302.5, 156], [292.5, 155]]},
        {"stripeIndex": 20, "segment": "right", "note": "G5", "polygon": [[287, 138.5], [298, 140], [313.5, 157], [302.5, 156]]},
        {"stripeIndex": 21, "segment": "right", "note": "Ab5", "polygon": [[298, 140], [308.5, 141.5], [324, 158], [313.5, 157]]},
        {"stripeIndex": 22, "segment": "right", "note": "A5", "polygon": [[308.5, 141.5], [318.5, 142.5], [334.5, 159], [324, 158]]},
        {"stripeIndex": 23, "segment": "right", "note": "Bb5", "polygon": [[318.5, 142.5], [328.5, 143.5], [345.5, 160], [334.5, 159]]},
        {"stripeIndex": 24, "segment": "right", "note": "B5", "polygon": [[328.5, 143.5], [339, 144.5], [355.5, 161], [345.5, 160]]},
        {"stripeIndex": 25, "segment": "right", "note": "C6", "polygon": [[339, 144.5], [349, 145.5], [366.5, 162], [355.5, 161]]},
    ],
}

STRIPE_COUNT = len(REFERENCE_CALIBRATION["stripes"])

# stripeIndex -> note, so the note binding is always taken from the reference
# and never from the model response.
NOTE_BY_INDEX = {s["stripeIndex"]: s["note"] for s in REFERENCE_CALIBRATION["stripes"]}
SEGMENT_BY_INDEX = {s["stripeIndex"]: s["segment"] for s in REFERENCE_CALIBRATION["stripes"]}
