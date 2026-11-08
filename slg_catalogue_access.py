"""Explicit build identity for catalogue compatibility routing (not authentication)."""
import os
import re
import sys

PRODUCT = 'SLGking'
PLATFORM = 'windows'
STABLE_VERSION = '0.29.0'
TEST_VERSION = '0.29.0'
HEADER_PRODUCT = 'X-SLGking-Product'
HEADER_PLATFORM = 'X-SLGking-Platform'
HEADER_VERSION = 'X-SLGking-Version'
HEADER_CHANNEL = 'X-SLGking-Channel'
_VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z', re.ASCII)


def build_identity(*, frozen=None, executable=None):
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else bool(frozen)
    executable = sys.executable if executable is None else executable
    # Unfrozen source is a development/test build; a renamed frozen executable
    # follows the same test filename convention as the existing desktop UI.
    channel = 'test' if not frozen or '_test' in os.path.basename(executable).lower() else 'stable'
    return {'product': PRODUCT, 'platform': PLATFORM, 'channel': channel,
            'version': TEST_VERSION if channel == 'test' else STABLE_VERSION}


def client_headers(*, frozen=None, executable=None):
    value = build_identity(frozen=frozen, executable=executable)
    return {HEADER_PRODUCT: value['product'], HEADER_PLATFORM: value['platform'],
            HEADER_VERSION: value['version'], HEADER_CHANNEL: value['channel']}


def vndb_eligible(version, channel):
    if not isinstance(version, str) or len(version) > 32 or not _VERSION.fullmatch(version):
        return False
    parts = tuple(map(int, version.split('.')))
    return ((channel == 'test' and parts >= (0, 28, 7))
            or (channel == 'stable' and parts >= (0, 29, 0)))
