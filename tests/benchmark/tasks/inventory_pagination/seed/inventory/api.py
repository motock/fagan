"""Read-only inventory catalog API.

The catalog is a fixed, deterministic list of 250 records. ``list_items``
returns a slice of it and does not paginate: a caller that needs more than
``limit`` records has no way to ask for the rest of the catalog.
"""

ITEMS = [
    {"id": i, "name": f"item-{i:03d}", "quantity": (i * 7) % 23}
    for i in range(1, 251)
]


def list_items(limit=100):
    """Return the first ``limit`` catalog records."""
    return ITEMS[:limit]
