"""Reporting helpers built on top of the inventory API."""
from inventory import api


def count_all():
    """Return the number of records in the catalog."""
    return len(api.list_items())
