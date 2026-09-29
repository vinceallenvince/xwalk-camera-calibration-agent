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

    def test_511ny_cameras_fetch_nysdot_stills_by_stream_id(self):
        """VIN-88: 511ny.org/map/Cctv/ may not survive the 2026-09-30 cutover,
        so the Manhattan cameras read Castle Rock stills keyed by stream ID."""
        for camera_id, stream_id in ((5056, "R11_272"), (5059, "R11_275"), (5072, "R11_279")):
            assert camera_config(camera_id).frame_url == (
                f"https://public.carsprogram.org/cameras/NYSDOT/{stream_id}.flv.png"
            )

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

    def test_bellevue_ids_are_8_plus_the_cctv_number(self):
        """VIN-84: BigQuery keys runs on an integer camera_id, so Bellevue's
        CCTV### IDs map to 8 + the number, zero-padded. The web app uses the
        same IDs; a mismatch publishes to a key nobody reads."""
        for camera_id in (80003, 80007, 80009, 80027):
            cctv = f"CCTV{camera_id - 80000:03d}"
            config = camera_config(camera_id)
            assert cctv in config.name
            assert f"/{cctv}L.stream/" in config.hls_url

    def test_new_bellevue_cameras_are_hls_cameras(self):
        """VIN-84: like 80007, none of these has a usable still."""
        for camera_id in (80003, 80009, 80027):
            config = camera_config(camera_id)
            assert config is CAMERAS[camera_id]
            assert "Bellevue" in config.name
            assert config.hls_url.endswith("/playlist.m3u8")
            assert config.snapshot_url is None

    def test_new_bellevue_scenes_route_a_ptz_turn_to_no_crosswalk(self):
        for camera_id in (80003, 80009, 80027):
            assert "no_crosswalk" in camera_config(camera_id).scene

    def test_80009_scene_says_bike_squares_are_not_stripes(self):
        """Green bike-crossing squares run beside two of 80009's crosswalks.
        Triage must not read them as paint that belongs to a crosswalk."""
        scene = camera_config(80009).scene
        assert "green squares" in scene
        assert "not crosswalk stripes" in scene

    def test_bellevue_scenes_name_their_fixed_obstructions(self):
        """A signal head or timestamp band that is always in frame is scene,
        not occlusion — otherwise every run reports degraded for it."""
        assert "signal head" in camera_config(80003).scene
        assert "signal head" in camera_config(80009).scene
        assert "timestamp band" in camera_config(80009).scene
        assert "timestamp band" in camera_config(80027).scene

    def test_80027_scene_still_reports_the_building_shadow(self):
        """Shadows cost stripes, so the scene describes 80027's building
        shadow without excusing it: on the paint it is still shadows."""
        assert "that is shadows" in camera_config(80027).scene

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

    def test_bellevue_crosswalk_ranks(self):
        """VIN-84: set from the first published runs. 80003 and 80009 read
        cleanly; 80027 picks up a parking-lane arrow and a building shadow."""
        assert camera_config(80003).crosswalk_rank == 2
        assert camera_config(80009).crosswalk_rank == 2
        assert camera_config(80027).crosswalk_rank == 3

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
        assert snapshot_url_for(5059) == (
            "https://public.carsprogram.org/cameras/NYSDOT/R11_275.flv.png"
        )
