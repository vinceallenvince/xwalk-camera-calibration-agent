"""Tests for the shape of the published calibration.

`current/camera_NNNN.json` is the only thing the web client reads, and it is
built from an allowlist rather than copied from the run record. That protects
the payload from accidentally widening, but it drifts the other way just as
quietly: VIN-46 removed the boundary polygons from the record and left them
named in the allowlist, so every run published three null keys until VIN-49.
These tests pin the shape from both directions.
"""

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

    def test_internal_fields_stay_out(self):
        """Timings, token usage and raw counts belong to BigQuery and the
        history archive, not to a payload fetched every 5 minutes."""
        payload = current_payload(RECORD)
        for internal in ("elapsed_ms", "gemini_tokens", "stripe_count",
                         "model", "published", "storage", "runId"):
            assert internal not in payload

    def test_every_allowlisted_key_exists_on_a_real_record(self):
        """Catches the drift direction that caused VIN-49: an allowlist entry
        naming a field the record no longer has, which publishes as null."""
        missing = [src for _, src in CURRENT_KEYS if src not in RECORD]
        assert missing == [], f"allowlist names fields absent from the record: {missing}"

    def test_shape_is_exactly_the_allowlist(self):
        assert set(current_payload(RECORD)) == {pub for pub, _ in CURRENT_KEYS}
