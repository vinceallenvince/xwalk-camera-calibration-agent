"""Tests for the shape of the published calibration.

`current/camera_NNNN.json` is the only thing the web client reads, and it is
built from an allowlist rather than copied from the run record. That protects
the payload from accidentally widening, but it drifts the other way just as
quietly: VIN-46 removed the boundary polygons from the record and left them
named in the allowlist, so every run published three null keys until VIN-49.
These tests pin the shape from both directions.
"""

import json
import sys

from app import persist
from app.persist import CURRENT_KEYS, current_payload

RECORD = {
    "runId": "run-20260821T120000Z-abc123",
    "cameraId": 5072,
    "createdAt": "2026-08-21T12:00:00+00:00",
    "status": "degraded",
    "reasoning": "Bus occluding the far crosswalk.",
    "conditions": {"crosswalkVisible": True, "occlusion": "vehicle"},
    "confidence": 0.9,
    "referenceFrame": {"width": 352, "height": 240},
    "stripes": [{"segment": "segment0", "stripeIndex": 0, "polygon": [[1, 2]]}],
    # Fields the client has no use for — these must not leak into current/.
    "elapsed_ms": 9326,
    "gemini_tokens": 1234,
    "stripe_count": 10,
    "model": "gemini-2.5-flash",
    "published": True,
    "storage": {"bigquery": "..."},
}


class TestPublishedShape:
    def test_no_boundary_keys(self):
        """The Option C contract: boundaries are absent, not null. The client
        hulls each segment's stripes instead."""
        payload = current_payload(RECORD)
        for dead in ("crosswalks", "leftCrosswalk", "rightCrosswalk"):
            assert dead not in payload

    def test_carries_what_the_client_reads(self):
        payload = current_payload(RECORD)
        assert payload["cameraId"] == 5072
        assert payload["updatedAt"] == RECORD["createdAt"]
        assert payload["status"] == "degraded"
        assert payload["referenceFrame"] == {"width": 352, "height": 240}
        assert payload["stripes"] == RECORD["stripes"]

    def test_carries_the_run_id(self):
        """Correlates a published calibration with its BigQuery row and
        history JSON — otherwise only matchable by timestamp."""
        assert current_payload(RECORD)["runId"] == RECORD["runId"]

    def test_internal_fields_stay_out(self):
        """Timings, token usage and raw counts belong to BigQuery and the
        history archive, not to a payload fetched every 5 minutes."""
        payload = current_payload(RECORD)
        for internal in ("elapsed_ms", "gemini_tokens", "stripe_count",
                         "model", "published", "storage"):
            assert internal not in payload

    def test_every_allowlisted_key_exists_on_a_real_record(self):
        """Catches the drift direction that caused VIN-49: an allowlist entry
        naming a field the record no longer has, which publishes as null."""
        missing = [src for _, src in CURRENT_KEYS if src not in RECORD]
        assert missing == [], f"allowlist names fields absent from the record: {missing}"

    def test_shape_is_exactly_the_allowlist(self):
        assert set(current_payload(RECORD)) == {pub for pub, _ in CURRENT_KEYS}


class TestFrameUri:
    """The archived frame's location, so an operator can open the exact frame
    a calibration was computed from (VIN-50/VIN-51). It is passed in rather
    than read from the record: the extension is content-sniffed at upload
    time, so it cannot be derived from the run id."""

    URI = "gs://xwalk-keyboards-01/calibration/history/camera_5072/run-x.png"

    def test_published_when_a_frame_was_archived(self):
        assert current_payload(RECORD, self.URI)["frameUri"] == self.URI

    def test_omitted_entirely_when_no_frame_was_archived(self):
        """Not null — a reader must never be handed a path to an object that
        is not there. Happens when save() got no frame, or the upload failed."""
        for absent in (None, ""):
            assert "frameUri" not in current_payload(RECORD, absent)

    def test_omitted_by_default(self):
        assert "frameUri" not in current_payload(RECORD)

    def test_does_not_disturb_the_rest_of_the_payload(self):
        with_frame = current_payload(RECORD, self.URI)
        without = current_payload(RECORD)
        assert {k: v for k, v in with_frame.items() if k != "frameUri"} == without


class TestSaveWiring:
    """save() is what actually assembles the frame URI, and it is the part a
    unit test on current_payload cannot reach: the URI is computed during the
    history upload and threaded into the publish a few lines later. Stub GCS
    and BigQuery so that wiring is exercised without touching production.
    """

    @staticmethod
    def _stub(monkeypatch):
        uploads: dict[str, object] = {}

        class Blob:
            def __init__(self, name):
                self.name = name

            def upload_from_string(self, data, content_type=None):
                uploads[self.name] = data

        class Bucket:
            def blob(self, name):
                return Blob(name)

        class StorageClient:
            def __init__(self, *a, **k):
                pass

            def bucket(self, name):
                return Bucket()

        class BQClient:
            def __init__(self, *a, **k):
                pass

            def insert_rows_json(self, table, rows):
                return []

        import types

        from google import cloud

        storage_mod = types.SimpleNamespace(Client=StorageClient)
        bq_mod = types.SimpleNamespace(Client=BQClient)
        monkeypatch.setattr(cloud, "storage", storage_mod, raising=False)
        monkeypatch.setattr(cloud, "bigquery", bq_mod, raising=False)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)
        monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bq_mod)
        return uploads

    def _published(self, uploads):
        current = [k for k in uploads if k.startswith("calibration/current/")]
        assert len(current) == 1, f"expected one current/ write, got {current}"
        return json.loads(uploads[current[0]])

    def test_frame_uri_points_at_the_frame_this_run_archived(self, monkeypatch):
        uploads = self._stub(monkeypatch)
        persist.save({**RECORD, "published": True}, frame=b"\x89PNG\r\n\x1a\n rest")

        published = self._published(uploads)
        assert published["frameUri"].endswith(f"/{RECORD['runId']}.png")
        # The object it names must be one this same call actually wrote.
        archived = published["frameUri"].split(f"{persist.BUCKET}/", 1)[1]
        assert archived in uploads

    def test_jpeg_frames_are_named_with_their_own_extension(self, monkeypatch):
        """The extension is content-sniffed, which is the whole reason the
        path cannot be derived from the run id by a consumer."""
        uploads = self._stub(monkeypatch)
        persist.save({**RECORD, "published": True}, frame=b"\xff\xd8\xff\xe0 jpeg")

        assert self._published(uploads)["frameUri"].endswith(f"/{RECORD['runId']}.jpg")

    def test_no_frame_means_no_key(self, monkeypatch):
        uploads = self._stub(monkeypatch)
        persist.save({**RECORD, "published": True}, frame=None)

        assert "frameUri" not in self._published(uploads)

    def test_unpublished_runs_still_archive_but_write_no_current(self, monkeypatch):
        uploads = self._stub(monkeypatch)
        persist.save({**RECORD, "published": False}, frame=b"\x89PNG\r\n\x1a\n rest")

        assert not [k for k in uploads if k.startswith("calibration/current/")]
        assert [k for k in uploads if k.startswith("calibration/history/")]
