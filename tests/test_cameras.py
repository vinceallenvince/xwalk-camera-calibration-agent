"""Tests for per-camera configuration.

The load-bearing claim: the triage prompt must describe the camera actually
being calibrated. A new camera judged against another camera's scene
description would be flagged for not matching it.
"""

import pytest

from app.cameras import CAMERAS, CameraConfig, UnknownCamera, camera_config
from app.main import snapshot_url_for
from app.tools import conditions_instruction


class TestRegistry:
    def test_registered_camera_keeps_its_scene(self):
        config = camera_config(5056)
        assert config is CAMERAS[5056]
        assert "bollard median" in config.scene

    def test_registered_camera_uses_the_snapshot_template(self):
        assert camera_config(5056).frame_url == "https://511ny.org/map/Cctv/5056"

    def test_unregistered_camera_is_refused(self):
        """Only registered cameras calibrate; the endpoints turn this into a
        404 rather than triaging an unknown view against a generic scene."""
        with pytest.raises(UnknownCamera):
            camera_config(9999)

    def test_5059_is_registered_with_its_own_scene(self):
        config = camera_config(5059)
        assert config is CAMERAS[5059]
        assert "W. 23 St" in config.name
        assert "planted" in config.scene
        assert "large tree" in config.scene

    def test_5059_scene_calls_the_frame_edge_truncation_normal(self):
        """Both crosswalks run off the sides of 5059's frame. Without this in
        the scene, triage reads a permanently partial view as something wrong
        with the camera — but publishing is gated on detection, and a partial
        read is a correct read."""
        assert "continue past the left and right" in camera_config(5059).scene

    def test_5072_is_registered_with_its_own_scene(self):
        config = camera_config(5072)
        assert config is CAMERAS[5072]
        assert "Chambers St" in config.name
        assert "mounting" in config.scene
        assert "planted median" in config.scene

    def test_80007_is_registered_as_an_hls_camera(self):
        """VIN-80: Bellevue has no usable still, so the frame must come from
        its video stream — a still from another camera would calibrate the
        wrong view."""
        config = camera_config(80007)
        assert config is CAMERAS[80007]
        assert "Bellevue" in config.name
        assert config.hls_url == (
            "https://trafficcams.bellevuewa.gov:443/traffic-edge/"
            "CCTV007L.stream/playlist.m3u8"
        )
        assert config.snapshot_url is None

    def test_80007_scene_routes_a_ptz_turn_to_no_crosswalk(self):
        """A pan-tilt-zoom camera turned away must publish stripes: [] rather
        than geometry from the wrong view."""
        assert "no_crosswalk" in camera_config(80007).scene

    def test_still_cameras_have_no_stream(self):
        for camera_id in (5056, 5059, 5072):
            assert camera_config(camera_id).hls_url is None

    def test_registry_carries_no_geometry(self):
        """Segments are discovered from the detections every run, so a camera
        never declares a crosswalk count or a detection threshold. Adding one
        back would reintroduce the per-camera tuning VIN-44 deleted."""
        fields = set(CameraConfig.__dataclass_fields__)
        assert fields == {
            "camera_id", "name", "scene", "snapshot_url", "hls_url", "crosswalk_rank",
        }

    def test_crosswalk_rank_initial_assignments(self):
        """VIN-70: 5059 is rank 1 (best composition), 5072 and 5056 are 3."""
        assert camera_config(5059).crosswalk_rank == 1
        assert camera_config(5072).crosswalk_rank == 3
        assert camera_config(5056).crosswalk_rank == 3

    def test_explicit_snapshot_url_wins_over_the_template(self):
        config = CameraConfig(
            camera_id=1, name="test cam", scene="a scene",
            snapshot_url="https://example.com/cam.jpg",
        )
        assert config.frame_url == "https://example.com/cam.jpg"


class TestTriagePrompt:
    def test_prompt_carries_the_cameras_identity_and_scene(self):
        prompt = conditions_instruction(camera_config(5056))
        assert "View 5056" in prompt
        assert "bollard median" in prompt

    def test_prompt_is_not_judged_against_5056s_scene(self):
        """The bug this module exists to prevent: a camera triaged against
        another's "two crosswalks separated by a bollard median" would be
        flagged degraded for matching its own scene."""
        prompt = conditions_instruction(camera_config(80007))
        assert "5056" not in prompt
        assert "bollard" not in prompt
        assert "CCTV007" in prompt

    def test_prompt_carries_5059s_own_scene_not_a_neighbours(self):
        """5056, 5059, and 5072 all watch West Street and all show two
        crosswalks, which makes cross-contamination easy to miss: each must be
        judged against its own median."""
        prompt = conditions_instruction(camera_config(5059))
        assert "View 5059" in prompt
        assert "large tree" in prompt
        assert "bollard" not in prompt
        assert "mounting" not in prompt

    def test_prompt_keeps_the_status_contract(self):
        prompt = conditions_instruction(camera_config(5056))
        for status in ("ok", "degraded", "no_crosswalk", "feed_down"):
            assert status in prompt
        assert "needs_review" not in prompt

    def test_prompt_separates_occlusion_from_visibility(self):
        """Dusk and shadows hurt stripe detection more than parked cars do.
        The prompt must give the model language for lighting, independent of
        physical occlusion, and must not treat a streetlit night as dark."""
        prompt = conditions_instruction(camera_config(5056))
        assert "conditions.occlusion" in prompt
        assert "conditions.visibility" in prompt
        for factor in ("shadows", "dusk", "glare"):
            assert factor in prompt
        assert "streetlit" in prompt

    def test_prompt_routes_a_reaim_with_visible_paint_to_degraded(self):
        """A re-aimed camera still showing crosswalks is degraded, not
        no_crosswalk — publishing stays gated on detection, and operators
        read the cameraMoved field."""
        prompt = conditions_instruction(camera_config(5056))
        assert 'do NOT\nreport "no_crosswalk" while paint is in view' in prompt


class TestScheduledSnapshotUrl:
    def test_camera_resolves_through_the_registry(self):
        assert snapshot_url_for(5059) == "https://511ny.org/map/Cctv/5059"
