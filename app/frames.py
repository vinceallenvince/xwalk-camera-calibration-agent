"""Frame sources: how a scheduled run gets the current picture from a camera.

Two kinds of camera exist:

  still — an HTTP endpoint that returns a JPEG/PNG snapshot (511NY, and any
          camera on an explicit snapshot_url).
  hls   — a live HLS video stream with no usable still (Bellevue, VIN-80).
          The run walks playlist -> chunklist -> newest .ts segment and
          decodes one frame from it.

Which one a camera uses is declared in app/cameras.py (CameraConfig.hls_url).
Everything downstream of here sees the same thing either way: image bytes and
a MIME type.

HLS pulls are deliberately small: one segment (~10 s, ~600 KB) per run. The
city's player limits viewing to protect its bandwidth, and one segment is all
calibration needs.
"""

from urllib.parse import urljoin

import httpx

from app.cameras import CameraConfig

SEGMENT_TIMEOUT_S = 30


class FrameSourceError(Exception):
    """The camera's source could not produce a frame."""


def media_uris(playlist: str) -> list[str]:
    """The URI lines of an m3u8 playlist, in order — tags and blanks dropped."""
    return [
        line.strip()
        for line in playlist.splitlines()
        if line.strip() and not line.startswith("#")
    ]


def is_media_playlist(playlist: str) -> bool:
    """A media playlist lists segments (#EXTINF); a master lists variants."""
    return "#EXTINF" in playlist


async def newest_segment(client: httpx.AsyncClient, playlist_url: str) -> bytes:
    """Download the newest .ts segment the stream advertises.

    Follows one level of master playlist to its first variant (Wowza serves
    `playlist.m3u8` -> `chunklist_w<session>.m3u8`, with a new session ID per
    request, so the chunklist URL can never be cached). Relative URIs resolve
    against the playlist that named them.
    """
    resp = await client.get(playlist_url)
    resp.raise_for_status()
    url, text = playlist_url, resp.text

    if not is_media_playlist(text):
        variants = media_uris(text)
        if not variants:
            raise FrameSourceError("HLS master playlist lists no variants")
        url = urljoin(url, variants[0])
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text

    segments = media_uris(text)
    if not segments:
        raise FrameSourceError("HLS chunklist lists no segments")

    resp = await client.get(urljoin(url, segments[-1]), timeout=SEGMENT_TIMEOUT_S)
    resp.raise_for_status()
    return resp.content


def decode_frame(segment: bytes) -> bytes:
    """Decode the first keyframe of a .ts segment and return it as a PNG.

    Keyframes decode without reference to any other frame, so skipping
    non-keyframes is both faster and immune to a segment that starts mid-GOP.
    PNG rather than JPEG: lossless, so detection sees exactly what the stream
    carried, with no quality knob to tune — and at 640x360 it is a few hundred
    KB. Encoding goes through PyAV's own encoder, keeping Pillow out of the
    runtime image.
    """
    import io

    import av  # heavy import (bundled ffmpeg); only HLS cameras pay for it

    with av.open(io.BytesIO(segment), format="mpegts") as container:
        if not container.streams.video:
            raise FrameSourceError("HLS segment has no video stream")
        stream = container.streams.video[0]
        stream.codec_context.skip_frame = "NONKEY"
        frame = next(container.decode(stream), None)
    if frame is None:
        raise FrameSourceError("HLS segment decoded no frames")

    encoder = av.CodecContext.create("png", "w")
    encoder.width = frame.width
    encoder.height = frame.height
    encoder.pix_fmt = "rgb24"
    packets = encoder.encode(frame.reformat(format="rgb24")) + encoder.encode(None)
    png = b"".join(bytes(p) for p in packets)
    if not png:
        raise FrameSourceError("PNG encode produced no data")
    return png


async def fetch_frame(
    camera: CameraConfig,
    still_url: str,
    client: httpx.AsyncClient,
) -> tuple[bytes, str]:
    """The camera's current frame as (image bytes, MIME type).

    `still_url` is where a still camera's snapshot lives; the caller resolves
    it so the legacy CALIBRATION_SNAPSHOT_URL override keeps working.
    """
    if camera.hls_url:
        return decode_frame(await newest_segment(client, camera.hls_url)), "image/png"

    resp = await client.get(still_url)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "image/jpeg")
    return resp.content, "image/png" if "png" in content_type else "image/jpeg"
