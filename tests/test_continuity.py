"""Tests for run-over-run segment continuity.

The load-bearing claim: a segment name must mean the same physical crosswalk
from run to run, because the web client maps names to fixed pitch anchors.
These tests hide crosswalks and assert the survivors keep their names — and
that the run reports a regression instead of publishing a renamed keyboard.
"""

from app.continuity import bbox_iou, reconcile_segments

# Two crosswalks roughly matching View 5056's layout, in pixel space.
LEFT = [[27, 108], [193, 120], [188, 139], [1, 125]]
RIGHT = [[291, 132], [349, 137], [350, 155], [301, 150]]


def nudge(polygon, dx=2.0, dy=1.0):
    """A detector's frame-to-frame wobble: same crosswalk, slightly moved."""
    return [[x + dx, y + dy] for x, y in polygon]


class TestBboxIou:
    def test_identical_polygons_are_a_full_match(self):
        assert bbox_iou(LEFT, LEFT) == 1.0

    def test_disjoint_polygons_do_not_match(self):
        assert bbox_iou(LEFT, RIGHT) == 0.0

    def test_wobble_still_overlaps_strongly(self):
        assert bbox_iou(LEFT, nudge(LEFT)) > 0.8

    def test_degenerate_polygons_never_match(self):
        assert bbox_iou([[0, 0], [1, 1]], LEFT) == 0.0


class TestFirstSighting:
    def test_no_previous_passes_names_through(self):
        detected = {"left": LEFT, "right": RIGHT}
        reconciled, report = reconcile_segments(detected, None)
        assert reconciled == detected
        assert report == {"regression": False, "missing": [], "renamed": {}}

    def test_empty_previous_passes_names_through(self):
        reconciled, report = reconcile_segments({"left": LEFT}, {})
        assert reconciled == {"left": LEFT}
        assert report["regression"] is False


class TestContinuity:
    def test_stable_detection_keeps_names_without_noise(self):
        previous = {"left": LEFT, "right": RIGHT}
        detected = {"left": nudge(LEFT), "right": nudge(RIGHT)}
        reconciled, report = reconcile_segments(detected, previous)
        assert set(reconciled) == {"left", "right"}
        assert report == {"regression": False, "missing": [], "renamed": {}}

    def test_missing_left_boundary_does_not_rename_the_right(self):
        """The bug this module exists to prevent: a truck hides the left
        crosswalk, so the physically-right crosswalk is the leftmost detection
        and arrives positionally named "left". It must leave as "right"."""
        previous = {"left": LEFT, "right": RIGHT}
        detected = {"left": nudge(RIGHT)}  # positional naming got it wrong

        reconciled, report = reconcile_segments(detected, previous)

        assert reconciled == {"right": nudge(RIGHT)}
        assert report["regression"] is True
        assert report["missing"] == ["left"]
        assert report["renamed"] == {"left": "right"}

    def test_everything_missing_is_a_regression(self):
        """detect_boundaries failed entirely — the merged single-segment
        fallback must not overwrite a published two-crosswalk calibration."""
        reconciled, report = reconcile_segments({}, {"left": LEFT, "right": RIGHT})
        assert reconciled == {}
        assert report["regression"] is True
        assert report["missing"] == ["left", "right"]

    def test_reaimed_camera_matches_nothing(self):
        far_away = [[x + 1000, y + 1000] for x, y in LEFT]
        reconciled, report = reconcile_segments({"left": far_away}, {"left": LEFT, "right": RIGHT})
        # The new boundary keeps its positional name; both previous crosswalks
        # are gone, so the run is held.
        assert reconciled == {"left": far_away}
        assert report["regression"] is True
        assert report["missing"] == ["left", "right"]

    def test_new_crosswalk_alongside_matches_gets_a_free_name(self):
        """A repaint adds a crosswalk: matched boundaries keep their names and
        the newcomer takes the next positional slot instead of colliding."""
        middle = [[x + 120, y + 20] for x, y in RIGHT]
        previous = {"left": LEFT, "right": RIGHT}
        detected = {
            "left": nudge(LEFT),
            # Positional order put the newcomer at "right" and the real right
            # crosswalk at "segment3".
            "right": middle,
            "segment3": nudge(RIGHT),
        }

        reconciled, report = reconcile_segments(detected, previous)

        assert reconciled["left"] == nudge(LEFT)
        assert reconciled["right"] == nudge(RIGHT)
        # The newcomer overlaps nothing published, so it gets the next free
        # positional name rather than stealing "right".
        assert reconciled["segment3"] == middle
        assert report["regression"] is False
        assert report["renamed"] == {"segment3": "right"}

    def test_single_crosswalk_camera_round_trips(self):
        reconciled, report = reconcile_segments({"left": nudge(LEFT)}, {"left": LEFT})
        assert set(reconciled) == {"left"}
        assert report["regression"] is False
