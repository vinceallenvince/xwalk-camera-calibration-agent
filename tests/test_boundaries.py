"""Boundary capping: a camera's registered crosswalk count is a hard limit.

The scenario these tests guard against is real: a truck parked mid-crosswalk
split camera 5056's right crosswalk into two detected boundaries, both were
published (left, right, segment3), and boundary continuity then held every
subsequent clean two-boundary run as a regression — the phantom could only be
replaced by another frame fragmented the same way.
"""

import unittest

from app.tools import BOUNDARY_MIN_CONFIDENCE, confident_boundaries, largest_boundaries


def _det(x: float, width: float, height: float = 20, confidence: float = 0.9) -> dict:
    return {"x": x, "width": width, "height": height, "confidence": confidence}


class ConfidentBoundariesTests(unittest.TestCase):
    def test_default_threshold_is_the_module_constant(self):
        just_under = _det(50, 90, confidence=BOUNDARY_MIN_CONFIDENCE - 0.01)
        at_bar = _det(300, 50, confidence=BOUNDARY_MIN_CONFIDENCE)

        self.assertEqual(confident_boundaries([just_under, at_bar]), [at_bar])

    def test_per_camera_threshold_keeps_a_less_certain_crosswalk(self):
        """The 5072 regression: the zero-shot model reads that camera's far
        crosswalk at 0.55-0.79 in good light. The global 0.8 bar discarded it
        on every probe frame; the camera's own bar must be able to keep it."""
        near = _det(80, 95, confidence=0.84)
        far = _det(280, 60, confidence=0.72)

        self.assertEqual(confident_boundaries([near, far]), [near])
        self.assertEqual(confident_boundaries([near, far], min_confidence=0.5), [near, far])

    def test_narrow_fragments_are_dropped_at_any_confidence(self):
        sliver = _det(340, 13, confidence=0.99)
        self.assertEqual(confident_boundaries([sliver], min_confidence=0.5), [])

    def test_results_come_back_in_frame_order(self):
        right = _det(300, 50)
        left = _det(50, 90)
        self.assertEqual(confident_boundaries([right, left]), [left, right])


class LargestBoundariesTests(unittest.TestCase):
    def test_no_cap_passes_through(self):
        detections = [_det(50, 90), _det(220, 40), _det(300, 50)]
        self.assertEqual(largest_boundaries(detections, None), detections)

    def test_under_cap_passes_through(self):
        detections = [_det(50, 90), _det(300, 50)]
        self.assertEqual(largest_boundaries(detections, 2), detections)

    def test_fragment_is_dropped_not_published(self):
        """One full left crosswalk plus a truck-split right crosswalk: the
        smaller right fragment is the one that goes, and the survivors stay
        in left-to-right order so positional naming holds."""
        left = _det(50, 95)
        right_fragment = _det(225, 36)
        right = _det(310, 50)

        kept = largest_boundaries([left, right_fragment, right], 2)

        self.assertEqual(kept, [left, right])

    def test_survivors_are_resorted_by_x(self):
        """The largest boundaries win regardless of position, but the result
        must come back in frame order — segment names are positional."""
        small_left = _det(30, 35)
        big_middle = _det(180, 80)
        big_right = _det(320, 60)

        kept = largest_boundaries([small_left, big_middle, big_right], 2)

        self.assertEqual([d["x"] for d in kept], [180, 320])


if __name__ == "__main__":
    unittest.main()
