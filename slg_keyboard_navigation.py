"""Small bounded navigation intent; no event queue or Tk dependency."""
from dataclasses import dataclass


@dataclass
class NavigationIntent:
    total: int
    size: int
    target: int | None = None

    def move(self, delta, page, index=None):
        if self.target is None:
            self.target = ((page - 1) * self.size + index + delta if index is not None
                           else (page - 1) * self.size + (0 if delta > 0 else self.size - 1))
        else:
            self.target += delta
        self.target = max(0, min(self.total - 1, self.target)) if self.total > 0 else max(0, self.target)
        return self.target

    def turn(self, delta, page):
        base = self.target // self.size + 1 if self.target is not None else page
        pages = max(1, (self.total + self.size - 1) // self.size) if self.total > 0 else max(1, base + delta)
        self.target = (max(1, min(pages, base + delta)) - 1) * self.size
        return self.target

    @property
    def page(self):
        return None if self.target is None else self.target // self.size + 1


def visible_scroll_fraction(top, bottom, view_top, view_height, region_top, region_bottom):
    """Return None for an already visible row (no viewport movement)."""
    if top >= view_top and bottom <= view_top + view_height:
        return None
    destination = top if top < view_top else bottom - view_height
    extent = region_bottom - region_top
    return max(0., min(1., (destination - region_top) / extent)) if extent > 0 else None
