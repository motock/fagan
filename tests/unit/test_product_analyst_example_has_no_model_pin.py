import json
from pathlib import Path


def test_product_analyst_example_has_no_model_pin() -> None:
    markdown_path = (
        Path(__file__).resolve().parents[2] / "agents" / "product-analyst.md"
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    first_json_block = markdown.split("```json", 1)[1].split("```", 1)[0]
    plan = json.loads(first_json_block)

    model_pins = [
        story["model"]
        for epic in plan["epics"]
        for story in epic["stories"]
        if "model" in story
    ]

    assert not model_pins, f"Example stories must not pin a model: {model_pins}"
