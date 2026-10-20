"""Safe, bounded retrieval of Zyoudao WordPress featured images.

The caller supplies the existing source HTTP client. This module performs no
filesystem writes: it returns validated image bytes and a content-addressed
filename for the caller to store in the catalogue's normal cover directory.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
import re
from urllib.parse import urlsplit, urlunsplit


SOURCE_HOST = "zyoudao.net"
MAX_COVER_BYTES = 512 * 1024
MAX_COVER_WIDTH = 4096
MAX_COVER_HEIGHT = 4096
MAX_COVER_PIXELS = 16_777_216

_MIME_EXTENSIONS = {
    "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": ("jpg", b"\xff\xd8\xff"),
    "image/webp": ("webp", b"RIFF"),
}
_PREFERRED_SIZES = ("medium_large", "large", "medium", "source_url", "full",
                    "thumbnail")
_MEDIA_ID = re.compile(r"^[1-9][0-9]{0,11}$")


def _validated_same_host_url(value: object) -> str | None:
    """Return a canonical HTTPS Zyoudao URL or reject it before any request."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(ord(ch) < 0x20 or ch == "\\" for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except (TypeError, ValueError):
        return None
    if (parsed.scheme.casefold() != "https" or
            hostname not in (SOURCE_HOST, "www." + SOURCE_HOST) or
            parsed.username is not None or parsed.password is not None or
            port not in (None, 443) or not parsed.path.startswith("/")):
        return None
    # Drop unneeded query and fragment components. WordPress media URLs are
    # direct paths; retaining arbitrary parameters is unnecessary for covers.
    return urlunsplit(("https", SOURCE_HOST, parsed.path, "", ""))


def _media_image_url(media: object) -> str | None:
    if not isinstance(media, dict):
        return None
    details = media.get("media_details")
    sizes = details.get("sizes") if isinstance(details, dict) else None
    if isinstance(sizes, dict):
        for name in _PREFERRED_SIZES:
            size = sizes.get(name)
            candidate = size.get("source_url") if isinstance(size, dict) else None
            safe = _validated_same_host_url(candidate)
            if safe:
                return safe
    return _validated_same_host_url(media.get("source_url"))


def _dimensions_from_header(data: bytes, mime: str) -> tuple[int, int] | None:
    """Read raster dimensions without decoding the entire image."""
    if mime == "image/png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    if mime == "image/jpeg":
        if len(data) < 4 or data[:3] != b"\xff\xd8\xff":
            return None
        # SOF markers carrying frame dimensions. DHT/JPG/DAC markers are not
        # frame headers; restart and TEM markers have no length field.
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        pos = 2
        while pos < len(data):
            while pos < len(data) and data[pos] != 0xFF:
                pos += 1
            while pos < len(data) and data[pos] == 0xFF:
                pos += 1
            if pos >= len(data):
                return None
            marker = data[pos]
            pos += 1
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                if marker == 0xD9:
                    return None
                continue
            if marker == 0xDA:
                return None
            if pos + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[pos:pos + 2], "big")
            if segment_length < 2 or pos + segment_length > len(data):
                return None
            if marker in sof:
                if segment_length < 7:
                    return None
                height = int.from_bytes(data[pos + 3:pos + 5], "big")
                width = int.from_bytes(data[pos + 5:pos + 7], "big")
                return width, height
            pos += segment_length
        return None

    if mime == "image/webp":
        if len(data) < 21 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            return None
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            b1, b2, b3, b4 = data[21:25]
            width = 1 + b1 + ((b2 & 0x3F) << 8)
            height = 1 + ((b2 >> 6) & 0x03) + (b3 << 2) + ((b4 & 0x0F) << 10)
            return width, height
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
    return None


def _valid_image(data: bytes, content_type: str) -> tuple[str, int, int] | None:
    mime = (content_type or "").split(";", 1)[0].strip().casefold()
    spec = _MIME_EXTENSIONS.get(mime)
    if not spec or not data or len(data) > MAX_COVER_BYTES:
        return None
    extension, signature = spec
    if mime == "image/webp":
        if len(data) < 12 or data[:4] != signature or data[8:12] != b"WEBP":
            return None
    elif not data.startswith(signature):
        return None
    dimensions = _dimensions_from_header(data, mime)
    if not dimensions:
        return None
    width, height = dimensions
    if (width <= 0 or height <= 0 or width > MAX_COVER_WIDTH or
            height > MAX_COVER_HEIGHT or width * height > MAX_COVER_PIXELS):
        return None

    # Pillow is optional in server tooling. When installed, decode and verify
    # the complete file so truncated/corrupt payloads are rejected too.
    try:
        from PIL import Image
    except ImportError:
        return extension, width, height
    expected_format = {"image/png": "PNG", "image/jpeg": "JPEG",
                       "image/webp": "WEBP"}[mime]
    try:
        with Image.open(BytesIO(data)) as image:
            if (image.format != expected_format or image.size != (width, height) or
                    getattr(image, "n_frames", 1) != 1):
                return None
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
    except Exception:
        return None
    return extension, width, height


def fetch_featured_cover(client, media_id) -> tuple[str, bytes] | None:
    """Fetch a validated WordPress featured image from Zyoudao.

    ``client`` must provide ``get(url, host, accept) -> (content_type, bytes)``
    and must refuse redirects (as ``slg_sources.PublicSourceClient`` does).
    Metadata URLs are checked before the image request; the client request is
    always pinned to ``zyoudao.net``. Any network, parsing, or validation error
    returns ``None`` without logging URLs or modifying files.
    """
    if isinstance(media_id, bool):
        return None
    media_text = str(media_id).strip()
    if not _MEDIA_ID.fullmatch(media_text):
        return None
    metadata_url = "https://%s/wp-json/wp/v2/media/%s" % (SOURCE_HOST, media_text)
    try:
        metadata_type, metadata_raw = client.get(
            metadata_url, SOURCE_HOST, "application/json")
        if "json" not in (metadata_type or "").casefold():
            return None
        if not isinstance(metadata_raw, (bytes, bytearray)) or len(metadata_raw) > MAX_COVER_BYTES:
            return None
        # This WordPress endpoint, like the posts feed, can prefix otherwise
        # valid JSON with a UTF-8 BOM.
        media = json.loads(bytes(metadata_raw).decode("utf-8-sig", "replace"))
        media_mime = (media.get("mime_type") or "").split(";", 1)[0].strip().casefold()
        if media_mime and media_mime not in _MIME_EXTENSIONS:
            return None
        image_url = _media_image_url(media)
        if not image_url:
            return None
        image_type, image_raw = client.get(
            image_url, SOURCE_HOST,
            "image/png,image/jpeg,image/webp")
        if not isinstance(image_raw, (bytes, bytearray)):
            return None
        payload = bytes(image_raw)
        validated = _valid_image(payload, image_type)
        if not validated:
            return None
        extension, _width, _height = validated
        filename = "cover-%s.%s" % (sha256(payload).hexdigest()[:24], extension)
        return filename, payload
    except Exception:
        # Errors are intentionally quiet: source URLs and server internals must
        # not leak into logs, while a missing cover must not break ingestion.
        return None
