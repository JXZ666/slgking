"""Explicit clipboard cover import. No Tk, network, persistence or polling.

Run ``prepare_clipboard_cover`` in a worker after an explicit user action.
The returned detached RGB image belongs to the caller and must be closed.
"""
from io import BytesIO
import ntpath
import os

from PIL import Image, ImageGrab, UnidentifiedImageError

from slg_local_library import MAX_COVER_BYTES, prepare_cover, prepare_cover_image


def _local_image_path(value):
    try:
        path = os.fspath(value)
    except TypeError as exc:
        raise ValueError("剪贴板中不是有效的本地图片文件。") from exc
    if (not isinstance(path, str) or not path or "\x00" in path
            or path.startswith(("\\\\", "//"))
            or "://" in path
            or not (os.path.isabs(path) or ntpath.isabs(path))):
        raise ValueError("请复制单个本地图片文件，不支持网址或网络路径。")
    if not os.path.isfile(path):
        raise ValueError("剪贴板中的图片文件已不存在或无法读取。")
    return path


def prepare_clipboard_cover():
    """Read one bitmap or local image file and return a validated RGB image.

    Windows clipboard file lists must contain exactly one local file. Text
    URLs are never fetched. Clipboard bitmaps have a pixel limit, followed by
    a 5 MiB limit on their prepared JPEG representation, not a fictitious
    source-file byte size. No persistent temporary image is created.
    """
    try:
        content = ImageGrab.grabclipboard()
    except (OSError, RuntimeError, NotImplementedError) as exc:
        raise ValueError("暂时无法读取剪贴板，请重新复制图片后重试。") from exc
    if isinstance(content, (list, tuple)):
        if len(content) != 1:
            raise ValueError("请只复制一个本地图片文件后再粘贴。")
        return prepare_cover(_local_image_path(content[0]))
    if not isinstance(content, Image.Image):
        raise ValueError("剪贴板中没有可用图片，请复制图片或单个本地图片文件。")
    prepared = None
    try:
        prepared = prepare_cover_image(content)
        with BytesIO() as encoded:
            prepared.save(encoded, format="JPEG", quality=90)
            if encoded.tell() > MAX_COVER_BYTES:
                raise ValueError("处理后的封面图片不能超过 5 MB。")
        return prepared
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        if prepared is not None:
            prepared.close()
        raise ValueError("剪贴板图片无法读取或已损坏。") from exc
    except Exception:
        if prepared is not None:
            prepared.close()
        raise
    finally:
        content.close()
