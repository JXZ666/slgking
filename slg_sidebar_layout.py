"""Sidebar metrics in logical pixels; Tk scaling belongs to the caller."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SidebarLayout:
    name: str
    width: int
    row_height: int
    font_size: int


COMPACT = SidebarLayout("compact", 190, 32, 13)
NORMAL = SidebarLayout("normal", 206, 38, 14)
LARGE = SidebarLayout("large", 224, 44, 15)


def sidebar_layout(width: float, height: float) -> SidebarLayout:
    """Choose the initial profile from the available logical window size."""
    if width < 1100 or height < 800:
        return COMPACT
    if width >= 1600 and height >= 950:
        return LARGE
    return NORMAL


def sidebar_layout_with_hysteresis(
    width: float, height: float, previous: SidebarLayout | None,
    margin: float = 24,
) -> SidebarLayout:
    """Keep the previous profile within a symmetric threshold dead band.

    A compact profile leaves only after *both* dimensions clear their upper
    thresholds. A large profile leaves when *either* clears its lower threshold.
    Passing None uses the exact initial thresholds, without a dead band.
    """
    if margin < 0:
        raise ValueError("margin must be nonnegative")
    if previous is None:
        return sidebar_layout(width, height)
    if previous not in (COMPACT, NORMAL, LARGE):
        raise ValueError("unknown sidebar profile")
    compact_limit = margin if previous == COMPACT else -margin
    if width < 1100 + compact_limit or height < 800 + compact_limit:
        return COMPACT
    large_limit = -margin if previous == LARGE else margin
    if width >= 1600 + large_limit and height >= 950 + large_limit:
        return LARGE
    return NORMAL
