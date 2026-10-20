"""Private, device-only avatar files. No network or account persistence here."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_PIXELS = 24_000_000
MAX_AVATAR_BYTES = 64 * 1024
MAX_AVATAR_SIDE = 256


def identity_key(account_id=None, admin=False):
    # Include the role even if an administrator happens to share an account ID.
    payload = json.dumps(["admin" if admin else "account" if account_id else "guest",
                          str(account_id) if account_id else None], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def avatar_path(appdir, key):
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("invalid avatar identity key")
    return Path(appdir) / "avatars" / (key + ".png")


def decode_source(path):
    path = Path(path)
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("图片文件不能超过20 MiB")
    try:
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("请选择JPEG、PNG或WebP静态图片")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("不支持动画图片，请选择静态图片")
            if image.width * image.height > MAX_SOURCE_PIXELS:
                raise ValueError("图片不能超过2400万像素")
            image.load()
            result = ImageOps.exif_transpose(image).convert("RGB")
            result.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            # A fresh image strips ICC, EXIF and text metadata.
            clean = Image.new("RGB", result.size)
            clean.paste(result)
            return clean
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ValueError("无法读取图片，文件可能损坏") from exc


def encode_crop(image, box):
    if len(box) != 4 or not all(math.isfinite(float(v)) for v in box):
        raise ValueError("invalid crop coordinates")
    x0, y0, x1, y1 = map(float, box)
    if x0 < 0 or y0 < 0 or x1 > image.width or y1 > image.height or x1 <= x0 or abs((x1-x0)-(y1-y0)) > 1:
        raise ValueError("裁切区域必须是图片内的正方形")
    side = min(MAX_AVATAR_SIDE, max(1, round(x1-x0)))
    crop = image.convert("RGB").resize((side, side), Image.Resampling.LANCZOS, box=(x0,y0,x1,y1))
    # Palette conversion gives a deterministic hard bound without writing lossy JPEG as PNG.
    clean = Image.new("RGB", crop.size)
    clean.paste(crop)
    for colors in (256, 128, 64):
        output = io.BytesIO()
        clean.quantize(colors=colors).save(output, format="PNG", optimize=True)
        data = output.getvalue()
        if len(data) <= MAX_AVATAR_BYTES:
            return data
    raise ValueError("头像压缩失败")


def _validated(data):
    if not isinstance(data, bytes) or len(data) > MAX_AVATAR_BYTES:
        raise ValueError("头像数据过大或格式错误")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.width != image.height or not 0 < image.width <= MAX_AVATAR_SIDE or getattr(image,"n_frames",1) != 1:
                raise ValueError("头像必须为不超过256像素的方形PNG")
            if set(image.info) - {"transparency"}:
                raise ValueError("头像不能包含附加元数据")
            image.load()
            return image.convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("头像文件损坏") from exc


def save_avatar(appdir, key, data):
    _validated(data)
    target = avatar_path(appdir, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".avatar-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_avatar(appdir, key):
    target = avatar_path(appdir, key)
    if not target.exists():
        return None
    if target.stat().st_size > MAX_AVATAR_BYTES:
        raise ValueError("本地头像文件过大")
    return _validated(target.read_bytes())


def reset_avatar(appdir, key):
    avatar_path(appdir, key).unlink(missing_ok=True)
