"""The two-segment namespace form, ``<segment>/<segment>``.

Core treats a two-segment namespace as an opaque key. Nothing here splits,
parses or interprets its parts: the only question asked of a value is whether
it has the shape. ``pipeline.store_records.validate_namespace`` accepts it next
to the single-segment form, and each store decides what it serves.
"""

from __future__ import annotations

import re

#: One segment: a lower-case DNS-label-like name of at most 63 characters.
SEGMENT = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')

_SEPARATOR = '/'
_SEGMENT_COUNT = 2


def is_segmented_namespace(value: object) -> bool:
    """Return True when ``value`` is exactly two segments joined by one ``/``."""
    if not isinstance(value, str):
        return False
    parts = value.split(_SEPARATOR)
    return len(parts) == _SEGMENT_COUNT and all(
        SEGMENT.fullmatch(part) is not None for part in parts
    )
