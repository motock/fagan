from pathlib import Path

HEADING = "### On-device tier status"
REFERENCE = Path(__file__).resolve().parents[2] / "REFERENCE.md"


def _section_body() -> str:
    text = REFERENCE.read_text(encoding="utf-8")
    assert text.count(HEADING) == 1
    start = text.index(HEADING)
    return text[start : text.index("\n---\n", start)]


def test_should_document_on_device_status_once_citing_both_retros():
    body = _section_body()

    assert "harness-review_2026-09-29.md" in body
    assert "harness-review_2026-10-18.md" in body


def test_should_not_duplicate_on_device_status_heading():
    text = REFERENCE.read_text(encoding="utf-8")

    assert text.count(HEADING) == 1
