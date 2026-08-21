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

    @property
    def frame_url(self) -> str:
        return self.snapshot_url or SNAPSHOT_URL_TEMPLATE.format(camera_id=self.camera_id)


CAMERAS: dict[int, CameraConfig] = {
    5056: CameraConfig(
        camera_id=5056,
        name="511NY View 5056 (West Street at W. 34 St, Manhattan)",
        scene="This camera shows two crosswalks separated by a bollard median.",
    ),
    5072: CameraConfig(
        camera_id=5072,
        name="511NY View 5072 (West Street at Chambers St, Manhattan)",
        scene=(
            "This camera shows two crosswalks separated by a planted median "
            "with trees and bollards. Part of the camera's own mounting "
            "assembly may fill the top of the frame, and a foreground planter "
            "and sign pole sit between the camera and the near roadway — all "
            "of that is normal for this scene, not an obstruction or a feed "
            "problem."
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
