"""Tests for the camera-agnostic stripe geometry.

The load-bearing claim is that a stripe's index describes where it sits on the
crosswalk, not where it landed in the detection list. These tests occlude
stripes and assert the survivors keep the indexes they had.
"""

import math

from app.geometry import (
    assign_segments,
    centroid,
    estimate_pitch,
    index_stripes,
    point_in_polygon,
    principal_axis,
)

PITCH = 10.0
COUNT = 18


def stripe_at(x: float, y: float = 129.0, angle: float = 0.0) -> dict:
    """A small quad centred on (x, y), optionally rotated about the origin."""
    corners = [(x - 3, y - 6), (x + 3, y - 6), (x + 3, y + 6), (x - 3, y + 6)]
    if angle:
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        corners = [(cx * cos_a - cy * sin_a, cx * sin_a + cy * cos_a) for cx, cy in corners]
    return {"polygon": [[cx, cy] for cx, cy in corners]}


def crosswalk(angle: float = 0.0) -> list[list[float]]:
    corners = [(0.0, 118.0), (180.0, 118.0), (180.0, 140.0), (0.0, 140.0)]
    if angle:
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        corners = [(cx * cos_a - cy * sin_a, cx * sin_a + cy * cos_a) for cx, cy in corners]
    return [[cx, cy] for cx, cy in corners]


def full_run(angle: float = 0.0) -> list[dict]:
    return [stripe_at(5.0 + i * PITCH, angle=angle) for i in range(COUNT)]


def indexes(stripes: list[dict]) -> list[int]:
    return [s["stripeIndex"] for s in stripes]


class TestIndexing:
    def test_evenly_spaced_stripes_get_consecutive_indexes(self):
        result = index_stripes(crosswalk(), full_run())
        assert indexes(result) == list(range(COUNT))

    def test_half_slot_positions_do_not_collide(self):
        """Banker's rounding would fold 0.5/1.5 into the same pair of slots."""
        result = index_stripes(crosswalk(), full_run())
        assert len(set(indexes(result))) == COUNT

    def test_occluded_leading_stripes_do_not_shift_the_rest(self):
        """The bug this whole design exists to prevent.

        A vehicle covering the first three bars must not transpose the
        remaining fifteen down by three slots.
        """
        everything = index_stripes(crosswalk(), full_run())
        occluded = index_stripes(crosswalk(), full_run()[3:])

        assert indexes(occluded) == indexes(everything)[3:]

    def test_occluded_middle_stripes_leave_a_gap(self):
        partial = full_run()[:5] + full_run()[9:]
        result = index_stripes(crosswalk(), partial)

        assert indexes(result) == [0, 1, 2, 3, 4, 9, 10, 11, 12, 13, 14, 15, 16, 17]

    def test_sub_pitch_boundary_jitter_does_not_shift_indexes(self):
        """The boundary edge wanders a few pixels between runs; indexes must not.

        Phase correction absorbs any wobble smaller than one stripe pitch,
        which is the regime real boundary detections live in.
        """
        baseline = index_stripes(crosswalk(), full_run())

        for nudge in (-4.0, -2.0, 1.0, 3.0, 4.0):
            jittered = [[0.0 + nudge, 118.0], [180.0 - nudge, 118.0],
                        [180.0 - nudge, 140.0], [0.0 + nudge, 140.0]]
            assert indexes(index_stripes(jittered, full_run())) == indexes(baseline), (
                f"a {nudge}px boundary nudge shifted the indexes"
            )

    def test_full_pitch_boundary_shift_moves_indexes(self):
        """A known and unavoidable limit, pinned so it stays visible.

        If the boundary genuinely grows by a whole stripe pitch, there is no
        way to tell that from a crosswalk that has one more stripe at the
        leading edge, so every index moves by one. Sub-pitch noise is handled;
        this is not. It only matters if the client treats index 0 as a fixed
        musical anchor — worth knowing before it does.
        """
        tight = [[4.0, 118.0], [176.0, 118.0], [176.0, 140.0], [4.0, 140.0]]
        loose = [[-6.0, 118.0], [186.0, 118.0], [186.0, 140.0], [-6.0, 140.0]]

        tight_indexes = indexes(index_stripes(tight, full_run()))
        loose_indexes = indexes(index_stripes(loose, full_run()))

        assert tight_indexes != loose_indexes
        # Uniformly shifted, never scrambled — spacing is still correct.
        offset = loose_indexes[0] - tight_indexes[0]
        assert loose_indexes == [i + offset for i in tight_indexes]

    def test_works_on_a_tilted_crosswalk(self):
        """Real crosswalks run diagonally across the frame."""
        angle = math.radians(20)
        result = index_stripes(crosswalk(angle), full_run(angle))
        assert indexes(result) == list(range(COUNT))

    def test_single_stripe_is_indexed_without_a_pitch_to_measure(self):
        result = index_stripes(crosswalk(), [stripe_at(5.0)])
        assert len(result) == 1
        assert result[0]["stripeIndex"] >= 0

    def test_no_stripes_returns_empty(self):
        assert index_stripes(crosswalk(), []) == []

    def test_missing_boundary_falls_back_to_stripe_extent(self):
        result = index_stripes([], full_run())
        assert indexes(result) == list(range(COUNT))

    def test_original_fields_are_preserved(self):
        stripes = [{**stripe_at(5.0), "segment": "left", "confidence": 0.9}]
        result = index_stripes(crosswalk(), stripes)
        assert result[0]["segment"] == "left"
        assert result[0]["confidence"] == 0.9

    def test_indexes_are_unique_even_when_pitch_is_underestimated(self):
        """Two bars closer than the median pitch must still get distinct slots."""
        crowded = full_run() + [stripe_at(6.0)]
        result = index_stripes(crosswalk(), crowded)
        assert len(set(indexes(result))) == len(result)


class TestPitch:
    def test_median_gap_survives_a_missing_stripe(self):
        # A hole at 30 makes one 20-wide gap; the median ignores it.
        projections = [0.0, 10.0, 20.0, 40.0, 50.0, 60.0]
        assert estimate_pitch(projections, 60.0) == PITCH

    def test_two_stripes_spread_evenly(self):
        assert estimate_pitch([0.0, 30.0], 30.0) == 30.0

    def test_single_stripe_falls_back_to_span(self):
        assert estimate_pitch([5.0], 180.0) == 180.0

    def test_degenerate_span_still_returns_positive_pitch(self):
        assert estimate_pitch([5.0], 0.0) > 0


class TestAxis:
    def test_horizontal_polygon(self):
        axis = principal_axis(crosswalk())
        assert abs(abs(axis[0]) - 1.0) < 1e-6

    def test_tilted_polygon_follows_the_tilt(self):
        angle = math.radians(30)
        axis = principal_axis(crosswalk(angle))
        assert abs(abs(math.atan2(axis[1], axis[0])) - angle) < 1e-6

    def test_degenerate_polygon_does_not_crash(self):
        assert principal_axis([[1.0, 1.0]]) == (1.0, 0.0)


class TestSegments:
    LEFT = [[0.0, 118.0], [180.0, 118.0], [180.0, 140.0], [0.0, 140.0]]
    RIGHT = [[280.0, 130.0], [360.0, 130.0], [360.0, 160.0], [280.0, 160.0]]

    def test_detections_land_in_the_boundary_that_contains_them(self):
        detections = [{"x": 50.0, "y": 129.0}, {"x": 300.0, "y": 145.0}]
        grouped = assign_segments(detections, {"left": self.LEFT, "right": self.RIGHT})

        assert len(grouped["left"]) == 1
        assert len(grouped["right"]) == 1

    def test_detection_outside_every_boundary_goes_to_the_nearest(self):
        """Paint spilling past a tight boundary should not be discarded."""
        detections = [{"x": 190.0, "y": 129.0}]
        grouped = assign_segments(detections, {"left": self.LEFT, "right": self.RIGHT})

        assert len(grouped["left"]) == 1
        assert len(grouped["right"]) == 0

    def test_no_boundaries_groups_nothing(self):
        assert assign_segments([{"x": 1.0, "y": 1.0}], {}) == {}

    def test_every_detection_is_kept(self):
        detections = [{"x": float(x), "y": 129.0} for x in range(0, 360, 20)]
        grouped = assign_segments(detections, {"left": self.LEFT, "right": self.RIGHT})

        assert sum(len(v) for v in grouped.values()) == len(detections)


class TestPointInPolygon:
    SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]

    def test_inside(self):
        assert point_in_polygon((5.0, 5.0), self.SQUARE)

    def test_outside(self):
        assert not point_in_polygon((15.0, 5.0), self.SQUARE)


class TestCentroid:
    def test_mean_of_vertices(self):
        assert centroid([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]) == (5.0, 5.0)

    def test_empty_polygon(self):
        assert centroid([]) == (0.0, 0.0)
