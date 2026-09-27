"""Per-camera configuration.

The pipeline is camera-agnostic — geometry and persistence key on camera_id
alone — but two things genuinely differ per camera: where to fetch a frame
from, and what the scene should look like, which is the context the Gemini
triage prompt judges a frame against.

Only registered cameras calibrate. An unknown ID is refused (the endpoints
answer 404) rather than triaged against a generic scene: onboarding a camera
is one registry entry, and the registry is the list of what the agent serves.

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
    # City of Bellevue, WA (VIN-80, VIN-84). Bellevue's own IDs (CCTV007) are
    # not numeric and BigQuery stores camera_id as an integer, so each gets
    # an app-side ID shared with xwalk-keyboards: 8 followed by the CCTV
    # number zero-padded to four digits (CCTV007 -> 80007). None of these
    # cameras has a usable still: frames come from the stream.
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
    80003: CameraConfig(
        camera_id=80003,
        name="City of Bellevue CCTV003 (100th Ave NE at NE 8th St, Bellevue, WA)",
        hls_url=(
            "https://trafficcams.bellevuewa.gov:443/traffic-edge/"
            "CCTV003L.stream/playlist.m3u8"
        ),
        scene=(
            "This camera looks down on a four-way intersection with a painted "
            "zebra crosswalk on each side: one runs across the far side of "
            "the intersection, one runs diagonally down the left, one runs "
            "diagonally down the right, and the near crosswalk runs across "
            "the bottom of the frame and off its bottom edge, so seeing only "
            "part of it is normal. A traffic signal head mounted by the "
            "camera covers the lower right corner. Buildings, trees, parked "
            "cars, and lane markings are all part of the normal scene. "
            "This is a pan-tilt-zoom camera: if it has been turned so that no "
            "crosswalk is in view, that is no_crosswalk, not a feed problem."
        ),
    ),
    80009: CameraConfig(
        camera_id=80009,
        name="City of Bellevue CCTV009 (Bellevue Way NE at Main St, Bellevue, WA)",
        hls_url=(
            "https://trafficcams.bellevuewa.gov:443/traffic-edge/"
            "CCTV009L.stream/playlist.m3u8"
        ),
        scene=(
            "This camera looks down on a four-way intersection with a painted "
            "zebra crosswalk on each side: a large one across the foreground "
            "at the bottom of the frame, one running up the left, one across "
            "the far side, and one running diagonally down the right. Beside "
            "the left and right crosswalks runs a bike crossing of green "
            "squares; the green squares are bike-lane markings, not "
            "crosswalk stripes. A traffic signal head mounted by the camera "
            "covers part of the right side, and a black timestamp band runs "
            "along the bottom edge. Buildings, trees, parked cars, and lane "
            "markings are all part of the normal scene. "
            "This is a pan-tilt-zoom camera: if it has been turned so that no "
            "crosswalk is in view, that is no_crosswalk, not a feed problem."
        ),
    ),
    80027: CameraConfig(
        camera_id=80027,
        name="City of Bellevue CCTV027 (110th Ave NE at NE 8th St, Bellevue, WA)",
        hls_url=(
            "https://trafficcams.bellevuewa.gov:443/traffic-edge/"
            "CCTV027L.stream/playlist.m3u8"
        ),
        scene=(
            "This camera looks down on an intersection with painted zebra "
            "crosswalks on the left, across the far side, and diagonally down "
            "the right, plus part of a fourth at the bottom right running off "
            "the frame edge. The rooftop of the building the camera is "
            "mounted on fills the lower left corner, and a white timestamp "
            "band covers the bottom edge. In daylight a neighbouring "
            "building casts a hard shadow that splits the frame; when it "
            "falls across the paint, that is shadows. Buildings, trees, "
            "parked cars, and lane markings are all part of the normal scene. "
            "This is a pan-tilt-zoom camera: if it has been turned so that no "
            "crosswalk is in view, that is no_crosswalk, not a feed problem."
        ),
    ),
}


class UnknownCamera(LookupError):
    """The camera ID is not in the registry."""


def camera_config(camera_id: int) -> CameraConfig:
    """The registered config. Raises UnknownCamera for an unregistered ID."""
    try:
        return CAMERAS[camera_id]
    except KeyError:
        raise UnknownCamera(camera_id) from None
