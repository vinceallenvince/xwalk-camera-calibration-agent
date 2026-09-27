"""Tests for the Roboflow detection filters.

The load-bearing claim: a stripe's width is not a reason to drop it. Stripes
that run sideways in frame (Bellevue 80007's near-left crossing) are wide
bars, and a width cap tuned on West Street's narrow vertical stripes silenced
that whole crossing (VIN-80).
"""

from app import tools


def _prediction(x: float, y: float, width: float, height: float, confidence: float = 0.9):
    half_w, half_h = width / 2, height / 2
    return {
        "x": x, "y": y, "width": width, "height": height, "confidence": confidence,
        "points": [
            {"x": x - half_w, "y": y - half_h},
            {"x": x + half_w, "y": y - half_h},
            {"x": x + half_w, "y": y + half_h},
            {"x": x - half_w, "y": y + half_h},
        ],
    }


def _stub_roboflow(monkeypatch, predictions):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"outputs": [{"predictions": {
                "image": {"width": 640, "height": 360},
                "predictions": predictions,
            }}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(tools.httpx, "Client", Client)


def test_wide_horizontal_stripes_are_kept(monkeypatch):
    bars = [_prediction(250, 150 + 25 * i, width=90, height=10) for i in range(6)]
    _stub_roboflow(monkeypatch, bars)

    result = tools.detect_stripes(b"frame")

    assert result["visible_count"] == 6


def test_short_and_low_confidence_detections_are_still_dropped(monkeypatch):
    _stub_roboflow(monkeypatch, [
        _prediction(100, 100, width=20, height=30),
        _prediction(200, 100, width=20, height=3),                    # too short
        _prediction(300, 100, width=20, height=30, confidence=0.2),   # too unsure
    ])

    assert tools.detect_stripes(b"frame")["visible_count"] == 1
