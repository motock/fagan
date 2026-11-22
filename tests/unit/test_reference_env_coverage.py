"""Every PIPELINE_* environment variable the code reads is named in REFERENCE.md.

An operator can only tune a variable the reference names, so a name that
pipeline/ or app/ reads but REFERENCE.md never mentions is undocumented. Names
that the pipeline sets for its own child processes are listed in the reference's
"Internal variables" paragraph, so this check needs no allowlist.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE = REPO_ROOT / "REFERENCE.md"
CODE_DIRS = ("pipeline", "app")

_ENV_LITERAL = re.compile(r'"(PIPELINE_[A-Z0-9_]+)"')


def _pipeline_env_names_in_code(root: Path) -> set[str]:
    names: set[str] = set()
    for directory in CODE_DIRS:
        for source in (root / directory).rglob("*.py"):
            names.update(_ENV_LITERAL.findall(source.read_text(encoding="utf-8")))
    return names


def _names_missing_from(names: set[str], reference_text: str) -> list[str]:
    return sorted(name for name in names if name not in reference_text)


def test_should_name_every_pipeline_env_var_the_code_reads() -> None:
    names = _pipeline_env_names_in_code(REPO_ROOT)
    missing = _names_missing_from(names, REFERENCE.read_text(encoding="utf-8"))
    assert not missing, (
        "REFERENCE.md does not name these PIPELINE_* variables that pipeline/ "
        "or app/ read: " + ", ".join(missing)
    )


def test_should_report_a_missing_name() -> None:
    reference_text = "| `PIPELINE_KNOWN_SETTING` | `1` | Documented here. |\n"
    missing = _names_missing_from(
        {"PIPELINE_KNOWN_SETTING", "PIPELINE_FAKE_UNDOCUMENTED_SETTING"},
        reference_text,
    )
    assert missing == ["PIPELINE_FAKE_UNDOCUMENTED_SETTING"]
