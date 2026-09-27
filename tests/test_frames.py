"""Tests for frame sources.

The load-bearing claims: an HLS camera's frame comes from the newest segment
its stream advertises (not the oldest, not a cached chunklist), and cameras on
stills keep fetching exactly as before. Playlists below are verbatim shapes
from Bellevue's Wowza server (VIN-80).
"""

import asyncio
import io

import httpx
import pytest

from app.cameras import CameraConfig, camera_config
from app.frames import (
    FrameSourceError,
    decode_frame,
    fetch_frame,
    is_media_playlist,
    media_uris,
    newest_segment,
)

BASE = "https://cams.example/traffic-edge/CCTV007L.stream/"

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=483525,CODECS="avc1.64001e",RESOLUTION=640x360
chunklist_w1409299498.m3u8
"""

CHUNKLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:11
#EXT-X-MEDIA-SEQUENCE:1637
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXTINF:10.501,
media_w1409299498_1637.ts
#EXTINF:9.451,
media_w1409299498_1638.ts
#EXTINF:10.501,
media_w1409299498_1639.ts
"""


def _client(routes: dict[str, httpx.Response], requested: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        return routes.get(url, httpx.Response(404))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


class TestPlaylistParsing:
    def test_media_uris_drops_tags_and_blanks(self):
        assert media_uris(CHUNKLIST) == [
            "media_w1409299498_1637.ts",
            "media_w1409299498_1638.ts",
            "media_w1409299498_1639.ts",
        ]

    def test_master_and_media_playlists_are_told_apart(self):
        assert not is_media_playlist(MASTER)
        assert is_media_playlist(CHUNKLIST)


class TestNewestSegment:
    def test_follows_master_to_chunklist_to_newest_segment(self):
        requested: list[str] = []
        routes = {
            BASE + "playlist.m3u8": httpx.Response(200, text=MASTER),
            BASE + "chunklist_w1409299498.m3u8": httpx.Response(200, text=CHUNKLIST),
            BASE + "media_w1409299498_1639.ts": httpx.Response(200, content=b"newest"),
        }

        async def go():
            async with _client(routes, requested) as client:
                return await newest_segment(client, BASE + "playlist.m3u8")

        assert _run(go()) == b"newest"
        # One segment per run: the city's server should see three small
        # requests, not a walk of the whole window.
        assert requested == [
            BASE + "playlist.m3u8",
            BASE + "chunklist_w1409299498.m3u8",
            BASE + "media_w1409299498_1639.ts",
        ]

    def test_a_media_playlist_url_is_used_directly(self):
        requested: list[str] = []
        routes = {
            BASE + "chunklist.m3u8": httpx.Response(200, text=CHUNKLIST),
            BASE + "media_w1409299498_1639.ts": httpx.Response(200, content=b"newest"),
        }

        async def go():
            async with _client(routes, requested) as client:
                return await newest_segment(client, BASE + "chunklist.m3u8")

        assert _run(go()) == b"newest"
        assert len(requested) == 2

    def test_empty_chunklist_is_a_frame_source_error(self):
        routes = {BASE + "chunklist.m3u8": httpx.Response(200, text="#EXTM3U\n#EXTINF:1,\n")}

        async def go():
            async with _client(routes, []) as client:
                return await newest_segment(client, BASE + "chunklist.m3u8")

        with pytest.raises(FrameSourceError):
            _run(go())

    def test_http_errors_propagate(self):
        async def go():
            async with _client({}, []) as client:
                return await newest_segment(client, BASE + "playlist.m3u8")

        with pytest.raises(httpx.HTTPStatusError):
            _run(go())


class TestFetchFrame:
    def test_still_cameras_fetch_the_still_url(self):
        requested: list[str] = []
        still = "https://511ny.org/map/Cctv/5056"
        routes = {still: httpx.Response(200, content=b"jpegbytes", headers={"content-type": "image/jpeg"})}

        async def go():
            async with _client(routes, requested) as client:
                return await fetch_frame(camera_config(5056), still, client)

        assert _run(go()) == (b"jpegbytes", "image/jpeg")
        assert requested == [still]

    def test_png_stills_keep_their_mime(self):
        still = "https://example/still"
        routes = {still: httpx.Response(200, content=b"png", headers={"content-type": "image/png"})}

        async def go():
            async with _client(routes, []) as client:
                return await fetch_frame(camera_config(5056), still, client)

        assert _run(go())[1] == "image/png"

    def test_hls_cameras_decode_from_the_stream_and_ignore_the_still_url(self, monkeypatch):
        requested: list[str] = []
        camera = CameraConfig(camera_id=1, name="t", scene="s", hls_url=BASE + "playlist.m3u8")
        routes = {
            BASE + "playlist.m3u8": httpx.Response(200, text=MASTER),
            BASE + "chunklist_w1409299498.m3u8": httpx.Response(200, text=CHUNKLIST),
            BASE + "media_w1409299498_1639.ts": httpx.Response(200, content=b"segment"),
        }
        monkeypatch.setattr("app.frames.decode_frame", lambda seg: b"PNG:" + seg)

        async def go():
            async with _client(routes, requested) as client:
                return await fetch_frame(camera, "https://511ny.org/map/Cctv/1", client)

        assert _run(go()) == (b"PNG:segment", "image/png")
        assert not any("511ny" in url for url in requested)


def _synthetic_ts(width: int = 64, height: int = 36, frames: int = 10) -> bytes:
    """A tiny H.264 MPEG-TS clip, built in memory — no camera footage in-repo."""
    av = pytest.importorskip("av")
    buf = io.BytesIO()
    with av.open(buf, "w", format="mpegts") as out:
        stream = out.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        for _ in range(frames):
            frame = av.VideoFrame(width, height, "rgb24").reformat(format="yuv420p")
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


class TestDecodeFrame:
    def test_decodes_a_png_at_the_stream_resolution(self):
        from app.coords import sniff_image_size

        png = decode_frame(_synthetic_ts(64, 36))
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert sniff_image_size(png) == (64, 36)

    def test_garbage_is_not_a_frame(self):
        pytest.importorskip("av")
        with pytest.raises(Exception):
            decode_frame(b"\x00" * 4096)
