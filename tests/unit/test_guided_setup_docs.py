import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "guided-setup.md"
README = ROOT / "README.md"

FLAGS = [
    "--repo-root", "--data-dir", "--port", "--autonomy", "--role", "--provider",
    "--model", "--set KEY=VALUE", "--skip-notifications", "--skip-integrations",
    "--skip-services", "--skip-claude-assets", "--yes", "--dry-run",
]


def test_should_document_every_flag():
    text = DOC.read_text()
    assert [f for f in FLAGS if f not in text] == []


def test_should_link_guide_from_readme():
    assert "docs/guided-setup.md" in README.read_text()


def test_should_keep_readme_h2_headings_stable():
    headings = re.findall(r"^## .+$", README.read_text(), flags=re.MULTILINE)
    assert len(headings) == 11


def test_should_keep_one_line_install_right_after_quickstart():
    text = README.read_text()
    assert re.search(r"^## Quickstart\n+### One-line install", text, flags=re.MULTILINE)


def test_should_not_contain_personal_home_path():
    assert "/Users/" not in DOC.read_text()
