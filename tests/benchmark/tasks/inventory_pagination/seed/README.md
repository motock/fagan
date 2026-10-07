# Inventory

A tiny read-only inventory catalog.

## `inventory/api.py`

- `ITEMS` — the catalog: 250 deterministic records, each a dict with `id`,
  `name` and `quantity`.
- `list_items(limit=100)` — returns the first `limit` records as a list.

## `inventory/report.py`

- `count_all()` — returns the number of records in the catalog.

## Tests

`test_inventory.py` covers the catalog contents and the current API surface.
Run it with `pytest test_inventory.py`.
