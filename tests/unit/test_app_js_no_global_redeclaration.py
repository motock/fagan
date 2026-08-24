"""Guard against re-declaring browser globals at the top level of static/app.js.

A top-level ``const window = ...`` (or ``let``/``var`` for any browser global
like ``document``, ``navigator``, ``location``) parses fine under Node — where
the test shim sets ``globalThis.window`` as a plain property — but throws a
parse-time ``SyntaxError: Identifier 'X' has already been declared`` in a real
browser, aborting the entire script and breaking the dashboard UI. The Node
unit tests therefore cannot catch this class of regression; this source-level
assertion can.

See the ``const window = globalThis.window;`` regression introduced by the
Comms nav-toggle story (PR #412) that broke the dashboard in the browser while
every Node test stayed green.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"
APP_JS = STATIC / "app.js"

# Browser globals that already exist as bindings in a browser script scope.
# Re-declaring any of them with const/let/var at the top level is a SyntaxError.
_BROWSER_GLOBALS = {"window", "document", "navigator", "location", "self"}

# A top-level declaration in app.js starts at column 0 (the file uses 2-space
# indent for anything nested). Anchoring at start-of-line avoids false
# positives from inner-scoped shadowing inside functions/blocks.
_TOPLEVEL_DECL = re.compile(
    r"^(?:const|let|var)\s+(" + "|".join(_BROWSER_GLOBALS) + r")\b"
)


def _top_level_redeclarations(source: str) -> list[str]:
    hits = []
    for line in source.splitlines():
        m = _TOPLEVEL_DECL.match(line)
        if m:
            hits.append(line.strip())
    return hits


def test_app_js_does_not_redeclare_browser_globals_at_top_level() -> None:
    source = APP_JS.read_text()
    hits = _top_level_redeclarations(source)
    assert not hits, (
        "static/app.js re-declares a browser global at the top level, which "
        "throws a SyntaxError in the browser (but not under Node, so CI misses "
        "it) and breaks the dashboard UI: " + ", ".join(hits)
    )