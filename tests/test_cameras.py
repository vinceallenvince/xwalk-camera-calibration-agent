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

    def test_5072_is_registered_with_its_own_scene(self):
        config = camera_config(5072)
        assert config is CAMERAS[5072]
        assert "Chambers St" in config.name
        assert "mounting" in config.scene
        assert "planted median" in config.scene
        assert config.expected_crosswalks == 2

    def test_5072_lowers_the_boundary_confidence_bar(self):
        """The far crosswalk in 5072's wider view detects at 0.55-0.79 even
        in good light; the default 0.8 bar discarded it on every probe frame.
        See VIN-39."""
        assert camera_config(5072).boundary_min_confidence == 0.5

    def test_other_cameras_keep_the_default_boundary_bar(self):
        assert camera_config(5056).boundary_min_confidence is None
        assert camera_config(9999).boundary_min_confidence is None

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
        be flagged degraded for matching its own scene."""
        prompt = conditions_instruction(camera_config(4321))
        assert "5056" not in prompt
        assert "bollard" not in prompt
        assert "4321" in prompt

    def test_prompt_keeps_the_status_contract(self):
        prompt = conditions_instruction(camera_config(4321))
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
        assert snapshot_url_for(7000) == "https://511ny.org/map/Cctv/7000"
