"""SIDEBAR-2: the orphan config/workspace navs are gone from index.html.

The dashboard sidebar's SYSTEM group now owns the configuration and workspace
entries, so the two empty `<nav>` containers that used to sit beside
#plan-list in static/index.html are dead markup. This module pins the removal
and guards the markup that must survive it.
"""
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
INDEX_HTML = STATIC_DIR / "index.html"

REMOVED_IDS = ("config-nav", "workspace-nav")

KEPT_IDS = (
    "config-view",
    "workspace-view",
    "workspace-form",
    "workspace-path-input",
    "workspace-create",
    "workspace-select",
    "workspace-picker",
    "plan-list",
)


@pytest.fixture(scope="module")
def index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.mark.parametrize("element_id", REMOVED_IDS)
def test_orphan_nav_is_removed(index_html: str, element_id: str) -> None:
    assert f'id="{element_id}"' not in index_html, (
        f'static/index.html must not contain id="{element_id}": the sidebar '
        "SYSTEM group replaced that orphan nav container"
    )


@pytest.mark.parametrize("element_id", KEPT_IDS)
def test_kept_markup_survives(index_html: str, element_id: str) -> None:
    assert f'id="{element_id}"' in index_html, (
        f'static/index.html must still contain id="{element_id}"'
    )
