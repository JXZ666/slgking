"""Pure state for directory-driven add/edit form suggestions.

The GUI must call ``edit`` only for actual user changes, not when rendering
``values``.  A user-cleared field deliberately remains manual.  Tokens belong
to one state instance and one request, including repeated visits to a path.
"""
from collections.abc import Mapping
from dataclasses import dataclass


FIELDS = ("title", "developer", "engine", "version")


@dataclass(frozen=True)
class RequestToken:
    generation: int
    path: str


def _value(value):
    return "" if value is None else str(value)


class AddFormState:
    """Track empty, auto and manual values without accessing Tk or disk."""

    def __init__(self, initial=None):
        initial = initial or {}
        self._values = {field: _value(initial.get(field)) for field in FIELDS}
        self._sources = {field: "manual" if value else "empty"
                         for field, value in self._values.items()}
        self._generation = 0
        self._token = None
        self._pending = False
        self._last_info = None

    @property
    def values(self):
        return self._values.copy()

    @property
    def sources(self):
        return self._sources.copy()

    @property
    def pending(self):
        return self._pending

    @property
    def current_token(self):
        return self._token

    @property
    def current_path(self):
        return self._token.path if self._token else None

    @property
    def last_info(self):
        return None if self._last_info is None else self._last_info.copy()

    def begin_request(self, path):
        self._generation += 1
        self._token = RequestToken(self._generation, str(path))
        self._pending = True
        self._last_info = None
        for field in FIELDS:
            if self._sources[field] == "auto":
                self._values[field] = ""
                self._sources[field] = "empty"
        return self._token

    def edit(self, field, value):
        if field not in FIELDS:
            raise KeyError(field)
        self._values[field] = _value(value)
        self._sources[field] = "manual"

    @staticmethod
    def _normalize(info):
        if not isinstance(info, Mapping):
            raise TypeError("Recognition info must be a mapping")
        return {field: _value(info.get(field)).strip() for field in FIELDS}

    def apply(self, token, info):
        """Accept one active request; never replace manual values."""
        if token is not self._token or not self._pending:
            return False
        normalized = self._normalize(info)
        for field, value in normalized.items():
            if self._sources[field] != "manual":
                self._values[field] = value
                self._sources[field] = "auto" if value else "empty"
        self._last_info = normalized
        self._pending = False
        return True

    def force_apply(self, token, info=None):
        """Explicitly adopt current recognition after GUI confirmation.

        With no ``info``, use the last accepted result.  Missing recognized
        fields clear old values; stale tokens can never force an overwrite.
        """
        if token is not self._token or token is None:
            return False
        if info is None:
            if self._last_info is None:
                return False
            normalized = self._last_info.copy()
        else:
            normalized = self._normalize(info)
        self._values = normalized.copy()
        self._sources = {field: "auto" if value else "empty"
                         for field, value in normalized.items()}
        self._last_info = normalized
        self._pending = False
        return True

    def fail(self, token):
        if token is not self._token or not self._pending:
            return False
        self._pending = False
        self._last_info = None
        return True

    def invalidate(self):
        """Reject delayed results when closing or changing identity."""
        self._generation += 1
        self._token = None
        self._pending = False
        self._last_info = None
