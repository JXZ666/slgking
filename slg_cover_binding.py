"""Main-thread cover presentation: reset recycled labels and reserve geometry."""
import math


def fit_cover_size(source_size, bounds):
    """Contain a portrait/landscape image without changing its aspect ratio."""
    sw, sh = source_size
    bw, bh = bounds
    if not all(math.isfinite(v) and v > 0 for v in (sw, sh, bw, bh)):
        raise ValueError('cover dimensions must be positive finite values')
    ratio = min(bw / sw, bh / sh)
    return max(1, round(sw * ratio)), max(1, round(sh * ratio))


def bounded_cover_message(message, max_characters=32):
    """Keep an upstream error/URL from increasing a recycled card's height."""
    text = ' '.join(str(message or '').split())
    return text if len(text) <= max_characters else text[:max_characters - 1] + '…'


def bind_cover(widget, image, width, height, message=''):
    """Explicitly replace *both* image and text on every cover state transition.

    Failures are text-only rather than text next to a full-height placeholder.
    The bounded message fits small card regions instead of resizing the row.
    Caller owns generation/key checks and CTkImage creation on the Tk thread.
    """
    # Conservative CJK capacity, including narrow summary thumbnails.
    capacity = max(4, int(max(1, width - 12) / 14) * max(1, int((height - 8) / 20)))
    # A CTkLabel is a frame containing a native Label. Do not let that child's
    # natural text/image request change the reserved cover column on paging.
    reserve = getattr(widget, 'grid_propagate', None)
    if reserve is not None:
        reserve(False)
    widget.configure(image=image, text=bounded_cover_message(message, min(32, capacity)) if image is None else '',
                     width=width, height=height, anchor='center', compound='center',
                     wraplength=max(1, width - 12))
