"""Tests for per-camera configuration.

The load-bearing claim: the triage prompt must describe the camera actually
being calibrated. A new camera judged against another camera's scene
description would be flagged for not matching it.
"""

from app.cameras import CAMERAS, CameraConfig, camera_config
from app.main import snapshot_url_for
from app.tools import conditions_instruction


class TestRegistry:
    def test_registered_camera_keeps_its_scene(self):
        config = camera_config(5056)
        assert config is CAMERAS[5056]
        assert "bollard median" in config.scene

    def test_registered_camera_uses_the_snapshot_template(self):
        assert camera_config(5056).frame_url == "https://511ny.org/map/Cctv/5056"

    def test_unregistered_camera_gets_a_generic_config(self):
        config = camera_config(9999)
        assert config.camera_id == 9999
        assert "9999" in config.name
        assert config.frame_url == "https://511ny.org/map/Cctv/9999"

    def test_registered_camera_declares_its_crosswalk_count(self):
        assert camera_config(5056).expected_crosswalks == 2

    def test_unregistered_camera_has_no_crosswalk_cap(self):
        """Unknown cameras publish whatever the detector finds — a cap only
        makes sense once someone has looked at the scene and counted."""
        assert camera_config(9999).expected_crosswalks is None

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

    def test_unregistered_camera_is_not_judged_against_5056s_scene(self):
        """The bug this module exists to prevent: a single-crosswalk camera
        triaged against "two crosswalks separated by a bollard median" would
        be flagged needs_review for matching its own scene."""
        prompt = conditions_instruction(camera_config(4321))
        assert "5056" not in prompt
        assert "bollard" not in prompt
        assert "4321" in prompt

    def test_prompt_keeps_the_status_contract(self):
        prompt = conditions_instruction(camera_config(4321))
        for status in ("ok", "degraded", "no_crosswalk", "feed_down", "needs_review"):
            assert status in prompt


class TestScheduledSnapshotUrl:
    def test_camera_resolves_through_the_registry(self):
        assert snapshot_url_for(7000) == "https://511ny.org/map/Cctv/7000"
