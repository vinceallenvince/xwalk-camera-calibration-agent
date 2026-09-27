"""Per-camera configuration.

The pipeline is camera-agnostic — geometry and persistence key on camera_id
alone — but two things genuinely differ per camera: where to fetch a frame
from, and what the scene should look like, which is the context the Gemini
triage prompt judges a frame against.

Cameras not in the registry still work: they get a snapshot URL from the
511NY template and a generic scene description, so pointing the agent at a
new camera needs no code change — registering it just sharpens the triage.

There is deliberately no geometry here. Segments are discovered from the
detections each run (see geometry.place_stripes), so a camera never needs a
declared crosswalk count or a tuned detection threshold.
"""

import os
from dataclasses import dataclass

# 511NY exposes every camera's snapshot at the same path, keyed by view ID.
SNAPSHOT_URL_TEMPLATE = os.environ.get(
    "CALIBRATION_SNAPSHOT_URL_TEMPLATE",
    "https://511ny.org/map/Cctv/{camera_id}",
)


@dataclass(frozen=True)
class CameraConfig:
    camera_id: int
    # Human-readable identity, used verbatim in the triage prompt.
    name: str
    # What the frame should show when everything is normal — the triage
    # model compares the current frame against this description.
    scene: str
    # Explicit snapshot source; cameras on the 511NY template can omit it.
    snapshot_url: str | None = None
    # HLS playlist for cameras with no usable still. When set, the frame
    # comes from the stream (app/frames.py) and snapshot_url is unused.
    hls_url: str | None = None
    # Composition quality for homepage ordering (1 = best, 5 = worst).
    # The web app sorts camera links by this value ascending.
    crosswalk_rank: int = 3

    @property
    def frame_url(self) -> str:
        return self.snapshot_url or SNAPSHOT_URL_TEMPLATE.format(camera_id=self.camera_id)


CAMERAS: dict[int, CameraConfig] = {
    5056: CameraConfig(
        camera_id=5056,
        name="511NY View 5056 (West Street at W. 34 St, Manhattan)",
        scene="This camera shows two crosswalks separated by a bollard median.",
        crosswalk_rank=3,
    ),
    5059: CameraConfig(
        camera_id=5059,
        name="511NY View 5059 (West Street at W. 23 St, Manhattan)",
        crosswalk_rank=1,
        scene=(
            "This camera shows two crosswalks separated by a wide planted "
            "median carrying grass, a large tree, and a signal pole; the "
            "tree's canopy and the shadow it casts fall across the middle of "
            "the frame. Both crosswalks continue past the left and right "
            "edges of the frame, so seeing only part of each one is normal — "
            "all of that is the scene, not an obstruction or a feed problem."
        ),
    ),
    5072: CameraConfig(
        camera_id=5072,
        name="511NY View 5072 (West Street at Chambers St, Manhattan)",
        crosswalk_rank=3,
        scene=(
            "This camera shows two crosswalks separated by a planted median "
            "with trees and bollards. Part of the camera's own mounting "
            "assembly may fill the top of the frame, and a foreground planter "
            "and sign pole sit between the camera and the near roadway — all "
            "of that is normal for this scene, not an obstruction or a feed "
            "problem."
        ),
    ),
    # City of Bellevue, WA (VIN-80). Bellevue's own ID is CCTV007, which is
    # not numeric, so 80007 is an app-side ID shared with xwalk-keyboards.
    # There is no usable still for this view: frames come from the stream.
    80007: CameraConfig(
        camera_id=80007,
        name="City of Bellevue CCTV007 (Bellevue Way NE at NE 8th St, Bellevue, WA)",
        hls_url=(
            "https://trafficcams.bellevuewa.gov:443/traffic-edge/"
            "CCTV007L.stream/playlist.m3u8"
        ),
        scene=(
            "This camera looks down on a four-way intersection with a painted "
            "zebra crosswalk on each side, at different angles: one runs "
            "vertically down the near left of the frame, one runs across the "
            "top, one runs diagonally down the right, and the fourth is only "
            "partly visible at the bottom right, running off the frame edge. "
            "Buildings, trees, parked cars, and white lane markings in the "
            "middle of the intersection are all part of the normal scene. "
            "This is a pan-tilt-zoom camera: if it has been turned so that no "
            "crosswalk is in view, that is no_crosswalk, not a feed problem."
        ),
    ),
}

_GENERIC_SCENE = (
    "No scene description is registered for this camera. Judge only what is "
    "visible: one or more painted crosswalks may be in view."
)


def camera_config(camera_id: int) -> CameraConfig:
    """The registered config, or a generic one for an unregistered camera."""
    config = CAMERAS.get(camera_id)
    if config:
        return config
    return CameraConfig(
        camera_id=camera_id,
        name=f"traffic camera {camera_id}",
        scene=_GENERIC_SCENE,
    )
