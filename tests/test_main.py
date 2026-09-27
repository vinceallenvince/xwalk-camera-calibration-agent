"""Tests for the HTTP surface: only registered cameras calibrate."""

import pytest
from fastapi.testclient import TestClient

import app.main
from app.main import app as api

client = TestClient(api)


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.setattr(app.main, "API_KEY", None)


class TestUnknownCamera:
    def test_scheduled_run_refuses_an_unregistered_camera(self):
        response = client.post("/api/calibrate-scheduled?cameraId=9999")
        assert response.status_code == 404

    def test_upload_refuses_an_unregistered_camera(self):
        response = client.post(
            "/api/calibrate",
            data={"cameraId": "9999"},
            files={"frame": ("frame.png", b"not reached", "image/png")},
        )
        assert response.status_code == 404
