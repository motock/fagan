"""Acceptance-file tamper restore for scripts/local_agent_oracle.py. Split out
purely to keep local_agent_oracle.py under the project's line-count target.
The wrapper `_restore_tampered_oracle_files` stays in the original module and
passes its own snapshot and `CWD` in, so tests that monkeypatch either on the
original module keep working.
"""


def restore_tampered_oracle_files_impl(snapshot, cwd) -> str:
    restored = []
    for rel, original in snapshot.items():
        path = cwd / rel
        current = path.read_text() if path.exists() else None
        if current != original:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(original)
            restored.append(rel)
    if not restored:
        return ""
    names = ", ".join(restored)
    return (f"\n\nWARNING: {names} is the read-only acceptance suite and was "
            f"restored after being modified via bash. It must NOT be edited "
            f"or deleted by any means, including shell commands. Change the "
            f"implementation file instead.")
