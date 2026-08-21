"""Tests for the camera-agnostic stripe geometry.

Two load-bearing claims, both settled in VIN-44:

  1. Segments are discovered from the stripes themselves — a gap along the
     crosswalk axis larger than a fraction of the frame width starts a new
     crosswalk run. No boundary polygon, no registered crosswalk count.
  2. Indexes are ordinal within a segment, not positional on the paint. A
     stripe the model missed does not leave a hole; its neighbours renumber.
     That is a deliberate trade, so it is asserted rather than tolerated.
"""

import math

from app.geometry import (
    FALLBACK_FRAME_WIDTH,
    SEGMENT_GAP_FRACTION,
    centroid,
    place_stripes,
    principal_axis,
    project,
    segment_name,
)

PITCH = 10.0
FRAME_WIDTH = 352.0
# The gap that separates two crosswalk runs on a 352px frame: ~136px measured,
# comfortably past the 88px threshold.
RUN_GAP = 140.0


def stripe_at(x: float, y: float = 129.0, angle: float = 0.0) -> dict:
    """A small quad centred on (x, y), optionally rotated about the origin."""
    corners = [(x - 3, y - 6), (x + 3, y - 6), (x + 3, y + 6), (x - 3, y + 6)]
    if angle:
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        corners = [(cx * cos_a - cy * sin_a, cx * sin_a + cy * cos_a) for cx, cy in corners]
    return {"polygon": [[cx, cy] for cx, cy in corners]}


def run_of(count: int, start: float = 5.0, angle: float = 0.0) -> list[dict]:
    return [stripe_at(start + i * PITCH, angle=angle) for i in range(count)]


def placed(stripes: list[dict]) -> list[tuple[str, int]]:
    result = place_stripes(stripes, FRAME_WIDTH)
    return [(s["segment"], s["stripeIndex"]) for s in result]


def segments(stripes: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in place_stripes(stripes, FRAME_WIDTH):
        counts[s["segment"]] = counts.get(s["segment"], 0) + 1
    return counts


class TestClustering:
    def test_two_runs_split_at_the_median(self):
        stripes = run_of(6) + run_of(5, start=5.0 + RUN_GAP)
        assert segments(stripes) == {"segment0": 6, "segment1": 5}

    def test_one_run_stays_one_segment(self):
        assert segments(run_of(12)) == {"segment0": 12}

    def test_segments_are_named_along_the_axis(self):
        """segment0 is the first run encountered along the principal axis, so
        naming never depends on which crosswalk a camera happens to show."""
        stripes = run_of(3, start=5.0 + RUN_GAP) + run_of(3)
        result = place_stripes(stripes, FRAME_WIDTH)
        first = [s for s in result if s["segment"] == "segment0"]
        assert max(centroid(s["polygon"])[0] for s in first) < RUN_GAP

    def test_three_runs_get_a_third_segment(self):
        stripes = run_of(3) + run_of(3, start=200.0) + run_of(3, start=400.0)
        assert segments(stripes) == {"segment0": 3, "segment1": 3, "segment2": 3}

    def test_threshold_scales_with_frame_width(self):
        """The same scene at double resolution must split the same way."""
        small = run_of(4) + run_of(4, start=5.0 + RUN_GAP)
        big = [
            {"polygon": [[x * 2, y * 2] for x, y in s["polygon"]]}
            for s in small
        ]
        assert (
            [(s["segment"], s["stripeIndex"]) for s in place_stripes(big, FRAME_WIDTH * 2)]
            == placed(small)
        )

    def test_occlusion_hole_over_splits_one_run(self):
        """Accepted failure mode: a vehicle parked mid-run leaves a gap wider
        than the threshold, so one crosswalk reads as two segments. Measured on
        real frames (VIN-44) — asserted so the behaviour stays deliberate."""
        stripes = run_of(3) + run_of(3, start=5.0 + RUN_GAP)
        assert len(segments(stripes)) == 2

    def test_narrow_median_under_splits_two_runs(self):
        """The other accepted failure mode: crosswalks closer together than
        the threshold merge into one segment."""
        stripes = run_of(3) + run_of(3, start=5.0 + 40.0)
        assert segments(stripes) == {"segment0": 6}

    def test_no_frame_width_falls_back(self):
        stripes = run_of(3) + run_of(3, start=5.0 + RUN_GAP)
        assert place_stripes(stripes, None) == place_stripes(stripes, FALLBACK_FRAME_WIDTH)

    def test_threshold_constant_is_the_validated_one(self):
        """0.25 sits mid-plateau for both registered cameras (VIN-44 replay of
        341 archived runs). Changing it needs a fresh replay, not a hunch."""
        assert SEGMENT_GAP_FRACTION == 0.25


class TestIndexing:
    def test_indexes_are_contiguous_from_zero(self):
        assert placed(run_of(5)) == [("segment0", i) for i in range(5)]

    def test_each_segment_restarts_at_zero(self):
        stripes = run_of(4) + run_of(3, start=5.0 + RUN_GAP)
        assert placed(stripes) == [
            ("segment0", 0), ("segment0", 1), ("segment0", 2), ("segment0", 3),
            ("segment1", 0), ("segment1", 1), ("segment1", 2),
        ]

    def test_indexes_follow_the_axis_not_the_detection_order(self):
        shuffled = [stripe_at(x) for x in (35.0, 5.0, 25.0, 15.0)]
        result = place_stripes(shuffled, FRAME_WIDTH)
        xs = [centroid(s["polygon"])[0] for s in result]
        assert xs == sorted(xs)
        assert [s["stripeIndex"] for s in result] == [0, 1, 2, 3]

    def test_a_missing_stripe_renumbers_its_neighbours(self):
        """The deliberate cost of ordinal indexing: gaps are NOT preserved.
        Dropping the leading stripe shifts every index down by one."""
        full = run_of(5)
        assert placed(full[1:]) == [("segment0", i) for i in range(4)]

    def test_rotated_crosswalk_indexes_along_its_own_axis(self):
        result = placed(run_of(6, angle=0.3))
        assert result == [("segment0", i) for i in range(6)]

    def test_single_stripe(self):
        assert placed([stripe_at(5.0)]) == [("segment0", 0)]

    def test_no_stripes(self):
        assert place_stripes([], FRAME_WIDTH) == []

    def test_original_fields_survive(self):
        result = place_stripes([{"polygon": stripe_at(5.0)["polygon"], "confidence": 0.9}], FRAME_WIDTH)
        assert result[0]["confidence"] == 0.9


class TestAxisDirection:
    """An eigenvector is only defined up to sign, and after VIN-46 that sign
    orders the whole keyboard. These state the invariant rather than the
    implementation, because it is easy to lose again (VIN-48)."""

    def test_mirroring_in_y_does_not_reverse_the_keyboard(self):
        """A crosswalk tilting up-to-the-right must not name its segments
        right-to-left. Before the fix this reversed the whole run."""
        tilted = [stripe_at(5.0 + i * PITCH, y=120.0 + i * 0.8) for i in range(8)]
        mirrored = [
            {"polygon": [[x, -y] for x, y in s["polygon"]]} for s in tilted
        ]
        assert placed(mirrored) == placed(tilted)

    def test_near_horizontal_row_is_stable_under_noise(self):
        """Sub-pixel y variation leaves cxy noise-dominated, so its sign — and
        therefore the ordering — used to change run to run. Three archived
        5072 runs were already reversing this way in production."""
        import random

        for seed in range(24):
            rng = random.Random(seed)
            stripes = [
                stripe_at(5.0 + i * PITCH, y=120.0 + rng.uniform(-0.2, 0.2))
                for i in range(8)
            ]
            result = place_stripes(stripes, FRAME_WIDTH)
            xs = [centroid(s["polygon"])[0] for s in result]
            assert xs == sorted(xs), f"reversed on seed {seed}"
            assert result[0]["stripeIndex"] == 0

    def test_segment0_is_leftmost_when_a_tilted_frame_splits(self):
        stripes = (
            [stripe_at(5.0 + i * PITCH, y=120.0 + i * 0.8) for i in range(4)]
            + [stripe_at(5.0 + RUN_GAP + i * PITCH, y=132.0 + i * 0.8) for i in range(4)]
        )
        result = place_stripes(stripes, FRAME_WIDTH)
        first = [s for s in result if s["segment"] == "segment0"]
        assert max(centroid(s["polygon"])[0] for s in first) < RUN_GAP

    def test_axis_is_canonically_oriented(self):
        for cloud in (
            [[0, 0], [100, 8]],
            [[0, 0], [100, -8]],
            [[0, 0], [0, 100]],
            [[0, 0], [-100, -8]],
        ):
            vx, vy = principal_axis(cloud)
            assert vx > 1e-9 or (abs(vx) <= 1e-9 and vy >= 0), f"{cloud} -> {(vx, vy)}"


class TestPrimitives:
    def test_centroid_of_a_square(self):
        assert centroid([[0, 0], [10, 0], [10, 10], [0, 10]]) == (5.0, 5.0)

    def test_centroid_of_nothing(self):
        assert centroid([]) == (0.0, 0.0)

    def test_principal_axis_of_a_horizontal_run(self):
        axis = principal_axis([[0, 0], [100, 0], [100, 5], [0, 5]])
        assert abs(abs(axis[0]) - 1.0) < 1e-6

    def test_principal_axis_of_a_vertical_run(self):
        axis = principal_axis([[0, 0], [5, 0], [5, 100], [0, 100]])
        assert abs(abs(axis[1]) - 1.0) < 1e-6

    def test_projection_along_x(self):
        assert project((3.0, 4.0), (1.0, 0.0)) == 3.0

    def test_segment_names_are_positional(self):
        assert [segment_name(i) for i in range(3)] == ["segment0", "segment1", "segment2"]
