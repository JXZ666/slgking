"""Small UI-thread coordinator for account reads and shop repainting.

Workers only deliver tokens through the existing GUI queue. All methods here
run on the Tk thread. This class neither executes nor retries writes.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PageToken:
    identity_epoch: int
    page_epoch: int


@dataclass(frozen=True)
class ReadToken:
    identity_epoch: int
    serial: int
    key: str
    page: PageToken | None = None


class RefreshCoordinator:
    def __init__(self, after_idle, after_cancel, render):
        self._after_idle = after_idle
        self._after_cancel = after_cancel
        self._render = render
        self._identity = None
        self._identity_epoch = 0
        self._page_epoch = 0
        self._page_open = False
        self._closed = False
        self._serial = 0
        self._reads = {}
        self._dirty = set()
        self._signatures = {}
        self._job = None

    def set_identity(self, identity):
        """Use (mode, account_id, GUI identity generation), never credentials."""
        if self._closed or identity == self._identity:
            return False
        self.close_page()
        self._identity = identity
        self._identity_epoch += 1
        self._reads.clear()
        return True

    def open_page(self):
        """Call once per new skeleton, not on a click on the active shop."""
        if self._closed:
            return None
        self.close_page()
        self._page_open = True
        return self.capture_page()

    def capture_page(self):
        if self._closed or not self._page_open:
            return None
        return PageToken(self._identity_epoch, self._page_epoch)

    def page_is_current(self, token):
        return token is not None and token == self.capture_page()

    def close_page(self):
        self._page_open = False
        self._page_epoch += 1
        self._dirty.clear()
        self._signatures.clear()
        for key, token in list(self._reads.items()):
            if token.page is not None:
                del self._reads[key]
        job, self._job = self._job, None
        if job is not None:
            # Cancellation is best effort; the epoch guard also rejects a
            # callback already dispatched by Tk or surviving root teardown.
            try:
                self._after_cancel(job)
            except Exception:
                pass

    def close(self):
        self.close_page()
        self._reads.clear()
        self._closed = True

    def begin_read(self, key, *, page_bound=False):
        """None means join existing logical read; errors must finish it too."""
        if self._closed or key in self._reads:
            return None
        page = self.capture_page() if page_bound else None
        if page_bound and page is None:
            return None
        self._serial += 1
        token = ReadToken(self._identity_epoch, self._serial, key, page)
        self._reads[key] = token
        return token

    def finish_read(self, token):
        """Retire exactly this request; return whether its cache may be applied.

        Identity reads may populate cache while the shop is closed, but mark()
        cannot repaint it. Page-bound reads additionally expire with the page.
        """
        if self._closed or token is None or self._reads.get(token.key) != token:
            return False
        del self._reads[token.key]
        return (token.identity_epoch == self._identity_epoch
                and (token.page is None or self.page_is_current(token.page)))

    def invalidate_reads(self, *keys):
        """Retire pre-write GETs. Start fresh reads only after write confirms.

        Invoke at both write start and completion if reads may run during the
        write. Invalidated completions cannot retire a replacement request.
        """
        for key in keys:
            self._reads.pop(key, None)

    def changed(self, domain, signature):
        """Compare immutable projected UI state, never the whole /me payload."""
        if self._closed or not self._page_open:
            return False
        if domain in self._signatures and self._signatures[domain] == signature:
            return False
        self._signatures[domain] = signature
        return True

    def invalidate_views(self, *domains):
        """Allow a failed presentation to be retried without retiring reads."""
        for domain in domains:
            self._signatures.pop(domain, None)

    def mark(self, *domains):
        if self._closed or not self._page_open or not domains:
            return
        self._dirty.update(domains)
        if self._job is None:
            page = self.capture_page()
            self._job = self._after_idle(lambda: self._flush(page))

    def _flush(self, page):
        if not self.page_is_current(page):
            return
        self._job = None
        domains = frozenset(self._dirty)
        self._dirty.clear()
        if domains:
            self._render(domains)
