"""Pin THE RULE: a dispatched agent child never receives a raw operator credential.

The credential seam (``app/credentials.py``) makes an operator credential opaque:
a :class:`~app.credentials.SecretValue` refuses to print or serialise itself and
the raw string is reachable only through ``.reveal()``, called from one named
injection function per credential class. These tests are structural so that a
future change cannot quietly widen the seam.
"""

from __future__ import annotations

from pathlib import Path

from app.credentials import SecretValue
from pipeline.execution import _AGENT_SECRET_ENV, _local_agent_env

ROOT = Path(__file__).resolve().parents[2]

# Files allowed to call ``.reveal()`` on a SecretValue. Each one is the single
# injection point for its credential class. This allowlist may only SHRINK, or
# be extended by a deliberate review: adding a file means adding a new place
# where a raw operator credential is materialised in the process.
REVEAL_ALLOWLIST = {
    "app/credentials.py",
    "app/backend_claude.py",
    "pipeline/notification_email.py",
    "pipeline/notification_slack.py",
}

# Files allowed to construct a SecretValue. Only the seam builds credentials;
# everything else receives one and treats it as opaque.
SECRET_VALUE_ALLOWLIST = {"app/credentials.py"}

# The names the local agent child must never inherit. Pinned here as well as in
# ``pipeline.execution`` so a weakened denylist fails this test, not just a
# behavioural one.
DENYLISTED_NAMES = {
    "PIPELINE_ANTHROPIC_API_KEY",
    "GH_TOKEN",
    "SLACK_WEBHOOK_URL",
    "PIPELINE_NOTIFY_EMAIL_PASSWORD",
    "OLLAMA_API_KEY",
}


def _source_files() -> list[Path]:
    """Every Python source file under ``app/`` and ``pipeline/``."""
    return sorted(
        [*(ROOT / "app").rglob("*.py"), *(ROOT / "pipeline").rglob("*.py")]
    )


def _relative(path: Path) -> str:
    """Repo-relative POSIX path, the form the allowlists are written in."""
    return path.relative_to(ROOT).as_posix()


def _files_containing(needle: str) -> dict[str, str]:
    """Map repo-relative path -> source text for files containing ``needle``."""
    found: dict[str, str] = {}
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            found[_relative(path)] = text
    return found


def test_reveal_is_called_only_from_the_documented_injection_points() -> None:
    """``.reveal(`` appears only in the reviewed injection functions."""
    offending = sorted(set(_files_containing(".reveal(")) - REVEAL_ALLOWLIST)
    assert offending == [], (
        "raw credential values are materialised outside the reviewed injection "
        f"points: {offending}"
    )


def test_secret_value_is_constructed_only_by_the_seam() -> None:
    """Only ``app/credentials.py`` wraps a raw string in a SecretValue."""
    offending = sorted(set(_files_containing("SecretValue(")) - SECRET_VALUE_ALLOWLIST)
    assert offending == [], f"SecretValue constructed outside the seam: {offending}"


def test_agent_secret_denylist_still_names_every_credential_class() -> None:
    """The dispatched-agent denylist was not weakened."""
    missing = sorted(DENYLISTED_NAMES - set(_AGENT_SECRET_ENV))
    assert missing == [], f"credentials dropped from _AGENT_SECRET_ENV: {missing}"


def test_local_agent_env_strips_secrets_and_never_wraps_values(
    monkeypatch,
) -> None:
    """A child env holds no denylisted name and only plain ``str`` values."""
    monkeypatch.delenv("PIPELINE_CREDENTIALS_PLUGIN", raising=False)
    env = {
        "PIPELINE_ANTHROPIC_API_KEY": "sk-ant-1",
        "GH_TOKEN": "ghp_1",
        "SLACK_WEBHOOK_URL": "https://hooks/x",
        "PIPELINE_NOTIFY_EMAIL_PASSWORD": "pw",
        "OLLAMA_API_KEY": "ol-1",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }

    result = _local_agent_env(dict(env))

    assert result.keys().isdisjoint(DENYLISTED_NAMES), (
        f"denylisted names leaked to the child: {sorted(result.keys() & DENYLISTED_NAMES)}"
    )
    assert result["PATH"] == "/usr/bin"
    assert result["HOME"] == "/root"
    for name, value in result.items():
        # ``isinstance`` is not enough: a SecretValue subclassing str would pass.
        assert type(value) is str, f"{name} is a {type(value).__name__}, not a str"
        assert not isinstance(value, SecretValue), f"{name} still carries a SecretValue"


def test_reference_documents_the_credential_seam() -> None:
    """The operator-facing reference names the seam and the channel settings."""
    text = (ROOT / "REFERENCE.md").read_text(encoding="utf-8", errors="replace")
    for needle in ("PIPELINE_CREDENTIALS_PLUGIN", "password_ref", "webhook_url_ref"):
        assert needle in text, f"REFERENCE.md does not document {needle}"


def test_changelog_unreleased_announces_the_credential_provider() -> None:
    """The credential provider is announced under ``## [Unreleased]``."""
    lines = (ROOT / "CHANGELOG.md").read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    assert "## [Unreleased]" in lines, "CHANGELOG.md has no Unreleased section"
    start = lines.index("## [Unreleased]")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        block.append(line)
    text = "\n".join(block)

    assert "### Added" in block, "CHANGELOG.md Unreleased has no '### Added' heading"
    bullets = [line for line in block if line.startswith("- ")]
    matching = [
        line
        for line in bullets
        if "PIPELINE_CREDENTIALS_PLUGIN" in line
        and "password_ref" in line
        and "webhook_url_ref" in line
        and "off by default" in line
    ]
    assert matching, (
        "CHANGELOG.md Unreleased bullet does not announce the credential "
        f"provider with its channel settings; block was:\n{text}"
    )
