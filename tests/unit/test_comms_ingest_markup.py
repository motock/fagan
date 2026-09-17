"""Markup-only tests for the chat-ingest hand-off panel in static/index.html.

This story adds three elements to the END of ``<div class="comms-body"
id="comms-body">`` -- a label, a second ``.comms-input-row`` holding
``#ingest-plan-name`` / ``#ingest-plan-submit``, and a ``#ingest-plan-status``
live region -- as SIBLINGS of the pre-existing ``.comms-input-row`` that holds
``#comms-input`` / ``#comms-send``.  It is markup-only: no JS wiring (that is
the follow-on story that touches static/app/comms.js) and no CSS (the Comms
classes are reused deliberately, so static/style.css must not change).

Like tests/unit/test_dashboard_comms_markup.py these are text-level
membership / ordering / count checks -- no browser, no node.  index.html is a
cumulative artifact that sibling stories also edit, so nothing here pins a
hash of it or an exact total of elements; only this story's own ids, their
counts, and their position relative to fixed anchors are graded.
"""
import hashlib
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"
INDEX_HTML = STATIC / "index.html"
STYLE_CSS = STATIC / "style.css"
COMMS_CSS_GUARD = Path(__file__).resolve().parent / "test_dashboard_comms_css.py"

NEW_IDS = ("ingest-plan-name", "ingest-plan-submit", "ingest-plan-status")
PREEXISTING_IDS = ("comms-input", "comms-send", "comms-thread", "comms-landing")

# The three elements this story inserts, byte for byte (leading indentation is
# compared separately below, so the lines here are stripped).
EXPECTED_BLOCK_LINES = [
    '<label class="comms-sub" for="ingest-plan-name">Ingest a saved plan</label>',
    '<div class="comms-input-row">',
    # Updated for the ingest-picker select swap: #ingest-plan-name is now a <select>, not a text <input>.
    '<select id="ingest-plan-name" class="comms-input" aria-label="Plan name to ingest"></select>',
    (
        '<button id="ingest-plan-submit" class="comms-send" type="button" '
        'aria-label="Ingest plan" title="Ingest">INGEST</button>'
    ),
    "</div>",
    (
        '<div class="comms-sub" id="ingest-plan-status" role="status" '
        'aria-live="polite"></div>'
    ),
]


def _read_index() -> str:
    assert INDEX_HTML.exists(), "static/index.html must exist"
    return INDEX_HTML.read_text(encoding="utf-8")


def _read_css() -> str:
    assert STYLE_CSS.exists(), "static/style.css must exist"
    return STYLE_CSS.read_text(encoding="utf-8")


def _idx(html: str, needle: str) -> int:
    pos = html.find(needle)
    assert pos != -1, f"{needle!r} not found in static/index.html"
    return pos


def _first_input_row(html: str) -> int:
    return _idx(html, '<div class="comms-input-row">')


def _tag(html: str, tag: str, id_value: str) -> str:
    """Return the full opening tag of the element carrying ``id_value``."""
    m = re.search(rf"<{tag}\b[^>]*\bid=\"{re.escape(id_value)}\"[^>]*>", html)
    assert m, f'no <{tag} ... id="{id_value}"> opening tag found'
    return m.group(0)


# --- positive: the three new ids exist ------------------------------------

def test_index_html_has_ingest_plan_name():
    assert 'id="ingest-plan-name"' in _read_index()


def test_index_html_has_ingest_plan_submit():
    assert 'id="ingest-plan-submit"' in _read_index()


def test_index_html_has_ingest_plan_status():
    assert 'id="ingest-plan-status"' in _read_index()


# --- positive: the inserted block is byte-for-byte what the story specifies -

def test_inserted_block_matches_spec_line_for_line():
    """The three elements must be inserted exactly as specified (byte for
    byte, modulo the surrounding indentation)."""
    lines = [ln.strip() for ln in _read_index().splitlines()]
    for start in range(len(lines)):
        window = lines[start:start + len(EXPECTED_BLOCK_LINES)]
        if window == EXPECTED_BLOCK_LINES:
            return
    raise AssertionError(
        "static/index.html does not contain the specified ingest block "
        "verbatim; expected these consecutive lines (ignoring indentation):\n"
        + "\n".join(EXPECTED_BLOCK_LINES)
    )


# --- positive: submit control shape ---------------------------------------

def test_ingest_submit_is_a_button_with_type_button():
    html = _read_index()
    tag = _tag(html, "button", "ingest-plan-submit")
    assert 'type="button"' in tag, (
        f"#ingest-plan-submit must be a <button type=\"button\">; got {tag!r}"
    )


def test_ingest_submit_is_not_an_anchor_or_div():
    html = _read_index()
    assert not re.search(r'<a\b[^>]*id="ingest-plan-submit"', html), (
        "#ingest-plan-submit must not be an <a> element"
    )
    assert not re.search(r'<div\b[^>]*id="ingest-plan-submit"', html), (
        "#ingest-plan-submit must not be a <div> element"
    )


def test_ingest_submit_visible_text_is_exactly_ingest():
    html = _read_index()
    m = re.search(
        r'<button\b[^>]*id="ingest-plan-submit"[^>]*>(.*?)</button>', html, re.DOTALL
    )
    assert m, 'no <button id="ingest-plan-submit"> ... </button> found'
    assert m.group(1).strip() == "INGEST", (
        f"#ingest-plan-submit visible text must be exactly 'INGEST'; "
        f"got {m.group(1)!r}"
    )


def test_ingest_submit_reuses_comms_send_class():
    tag = _tag(_read_index(), "button", "ingest-plan-submit")
    assert 'class="comms-send"' in tag, (
        f"#ingest-plan-submit must reuse the .comms-send class; got {tag!r}"
    )


def test_ingest_submit_has_aria_label():
    tag = _tag(_read_index(), "button", "ingest-plan-submit")
    assert re.search(r'aria-label="[^"]+"', tag), (
        f"#ingest-plan-submit must carry a non-empty aria-label; got {tag!r}"
    )


# --- positive: name control shape -----------------------------------------

def test_ingest_name_is_a_select_element():
    tag = _tag(_read_index(), "select", "ingest-plan-name")
    assert tag.startswith("<select"), (
        f"#ingest-plan-name must be a <select>; got {tag!r}"
    )


def test_ingest_name_has_aria_label():
    tag = _tag(_read_index(), "select", "ingest-plan-name")
    assert re.search(r'aria-label="[^"]+"', tag), (
        f"#ingest-plan-name must carry a non-empty aria-label; got {tag!r}"
    )


def test_ingest_name_reuses_comms_input_class():
    tag = _tag(_read_index(), "select", "ingest-plan-name")
    assert 'class="comms-input"' in tag, (
        f"#ingest-plan-name must reuse the .comms-input class; got {tag!r}"
    )


# --- positive: status live region -----------------------------------------

def test_ingest_status_is_a_div_with_role_status_and_polite_live():
    tag = _tag(_read_index(), "div", "ingest-plan-status")
    assert 'role="status"' in tag, (
        f'#ingest-plan-status must carry role="status"; got {tag!r}'
    )
    assert 'aria-live="polite"' in tag, (
        f'#ingest-plan-status must carry aria-live="polite"; got {tag!r}'
    )


def test_ingest_status_is_empty():
    html = _read_index()
    m = re.search(r'<div\b[^>]*id="ingest-plan-status"[^>]*>(.*?)</div>', html, re.DOTALL)
    assert m, 'no <div id="ingest-plan-status"> ... </div> found'
    assert m.group(1).strip() == "", (
        f"#ingest-plan-status must start empty; got {m.group(1)!r}"
    )


def test_ingest_status_reuses_comms_sub_class():
    tag = _tag(_read_index(), "div", "ingest-plan-status")
    assert 'class="comms-sub"' in tag, (
        f"#ingest-plan-status must reuse the .comms-sub class; got {tag!r}"
    )


# --- positive: label wiring ------------------------------------------------

def test_ingest_label_for_matches_input_id():
    html = _read_index()
    m = re.search(r"<label\b[^>]*\bfor=\"([^\"]+)\"[^>]*>Ingest a saved plan</label>", html)
    assert m, (
        'no <label for="...">Ingest a saved plan</label> found in '
        "static/index.html"
    )
    assert m.group(1) == "ingest-plan-name", (
        f'label for attribute must be "ingest-plan-name"; got {m.group(1)!r}'
    )
    # ...and the input it points at really carries that id.
    assert 'id="ingest-plan-name"' in html


def test_ingest_label_reuses_comms_sub_class():
    html = _read_index()
    m = re.search(r"<label\b[^>]*>Ingest a saved plan</label>", html)
    assert m, "no <label>Ingest a saved plan</label> found"
    assert 'class="comms-sub"' in m.group(0), (
        f"the ingest label must reuse the .comms-sub class; got {m.group(0)!r}"
    )


# --- positive: ordering relative to fixed anchors --------------------------

def test_new_controls_come_after_the_existing_input_row():
    """#comms-landing < the FIRST .comms-input-row < #ingest-plan-name.

    tests/unit/test_comms_landing_redesign.py's _landing_block() captures
    everything between #comms-landing and the FIRST .comms-input-row, so the
    new row must not be inserted before the existing one.
    """
    html = _read_index()
    landing = _idx(html, 'id="comms-landing"')
    first_row = _first_input_row(html)
    name = _idx(html, 'id="ingest-plan-name"')
    assert landing < first_row < name, (
        "expected index(#comms-landing) < index(first .comms-input-row) < "
        f"index(#ingest-plan-name); got {landing}, {first_row}, {name}"
    )


def test_nothing_inserted_between_landing_and_the_existing_input_row():
    """The landing block must stay exactly as it was: no ingest markup may
    land between #comms-landing and the pre-existing input row."""
    html = _read_index()
    landing = _idx(html, 'id="comms-landing"')
    first_row = _first_input_row(html)
    between = html[landing:first_row]
    assert "ingest-plan" not in between, (
        "ingest markup must not be inserted between #comms-landing and the "
        "existing .comms-input-row"
    )


def test_new_controls_are_siblings_of_the_input_row_not_children():
    """Load-bearing: a '</div>' must appear between the first
    .comms-input-row opening tag and #ingest-plan-name -- that is the input
    row's own close, which pins the new controls as SIBLINGS of the row
    rather than children of it.  (The plain ordering assertion above passes
    either way, so this one is what actually grades the placement.)"""
    html = _read_index()
    first_row = _first_input_row(html)
    name = _idx(html, 'id="ingest-plan-name"')
    between = html[first_row:name]
    assert "</div>" in between, (
        "no '</div>' between the first .comms-input-row and "
        "#ingest-plan-name: the new controls are nested INSIDE the existing "
        "input row instead of being its siblings"
    )


def test_new_controls_sit_inside_the_comms_view():
    """<section id="comms-view"> < #comms-body < #ingest-plan-name."""
    html = _read_index()
    view = _idx(html, '<section id="comms-view"')
    body = _idx(html, 'id="comms-body"')
    name = _idx(html, 'id="ingest-plan-name"')
    assert view < body < name, (
        "expected index(<section id=\"comms-view\") < index(#comms-body) < "
        f"index(#ingest-plan-name); got {view}, {body}, {name}"
    )


def test_ingest_status_is_the_last_child_of_comms_body():
    """The three elements are the LAST children of #comms-body: after the
    status element's own close, only whitespace may precede #comms-body's
    close."""
    html = _read_index()
    status = _idx(html, 'id="ingest-plan-status"')
    status_close = html.index("</div>", status) + len("</div>")
    body_close = html.index("</div>", status_close)
    trailing = html[status_close:body_close]
    assert trailing.strip() == "", (
        "expected #ingest-plan-status to be the last child of #comms-body; "
        f"found {trailing!r} after it"
    )


def test_existing_comms_elements_keep_their_relative_order():
    """No existing element may be reordered: thread < landing < input row."""
    html = _read_index()
    thread = _idx(html, 'id="comms-thread"')
    landing = _idx(html, 'id="comms-landing"')
    first_row = _first_input_row(html)
    assert thread < landing < first_row, (
        "existing Comms elements were reordered; expected "
        f"index(#comms-thread) < index(#comms-landing) < index(input row); "
        f"got {thread}, {landing}, {first_row}"
    )


def test_no_script_tag_added_with_the_ingest_markup():
    """This story is markup-only: no <script> may be introduced alongside the
    new controls (the wiring story adds its import elsewhere)."""
    html = _read_index()
    first_row = _first_input_row(html)
    status = _idx(html, 'id="ingest-plan-status"')
    status_close = html.index("</div>", status) + len("</div>")
    assert "<script" not in html[first_row:status_close], (
        "no <script> tag may be added with the ingest markup"
    )


def test_a_second_comms_input_row_exists():
    """The new controls live in their OWN .comms-input-row, a sibling of the
    pre-existing one (a second row is intentional and harmless)."""
    html = _read_index()
    count = html.count('<div class="comms-input-row">')
    assert count >= 2, (
        "expected a second <div class=\"comms-input-row\"> for the ingest "
        f"controls; found {count}"
    )


def test_preexisting_input_row_controls_are_intact():
    """No existing id, class, attribute or text may change: the pre-existing
    row must still hold the #comms-input textarea and the TRANSMIT button."""
    html = _read_index()
    first_row = _first_input_row(html)
    name = _idx(html, 'id="ingest-plan-name"')
    row = html[first_row:name]
    assert 'id="comms-input"' in row, "the pre-existing row lost #comms-input"
    assert 'class="comms-input"' in row, "the pre-existing row lost .comms-input"
    assert 'id="comms-send"' in row, "the pre-existing row lost #comms-send"
    assert 'class="comms-send"' in row, "the pre-existing row lost .comms-send"
    assert "TRANSMIT" in row, "the pre-existing row lost its TRANSMIT label"


# --- negative: no duplicate ids -------------------------------------------

def test_new_ids_appear_exactly_once():
    html = _read_index()
    for id_value in NEW_IDS:
        count = html.count(f'id="{id_value}"')
        assert count == 1, (
            f'id="{id_value}" must appear exactly once in static/index.html; '
            f"found {count}"
        )


def test_preexisting_comms_ids_still_appear_exactly_once():
    html = _read_index()
    for id_value in PREEXISTING_IDS:
        count = html.count(f'id="{id_value}"')
        assert count == 1, (
            f'pre-existing id="{id_value}" must still appear exactly once in '
            f"static/index.html; found {count}"
        )


# --- negative: this story added no CSS ------------------------------------

def test_new_ids_are_absent_from_style_css():
    css = _read_css()
    for id_value in NEW_IDS:
        assert id_value not in css, (
            f"static/style.css must not reference #{id_value}: this story "
            "reuses the existing Comms classes and adds no CSS"
        )


def test_no_comms_ingest_rule_in_style_css():
    css = _read_css()
    assert ".comms-ingest" not in css, (
        "static/style.css must not gain a .comms-ingest rule: this story "
        "reuses the existing Comms classes and adds no CSS"
    )


# --- the authorized re-pin of the pre-existing index.html self-guard -------

def _index_digest() -> str:
    return hashlib.sha256(INDEX_HTML.read_bytes()).hexdigest()


def _pinned_index_digest() -> str:
    guard = COMMS_CSS_GUARD.read_text(encoding="utf-8")
    m = re.search(r'^INDEX_HTML_SHA256\s*=\s*"([0-9a-f]{64})"', guard, re.MULTILINE)
    assert m, (
        "tests/unit/test_dashboard_comms_css.py must still define "
        "INDEX_HTML_SHA256 as a 64-char hex string"
    )
    return m.group(1)


def test_index_html_sha256_pin_matches_the_current_file():
    """This story legitimately changes index.html, so the pre-existing
    INDEX_HTML_SHA256 self-guard in tests/unit/test_dashboard_comms_css.py
    must be re-pinned to the new digest (the one authorized edit to that
    file)."""
    pinned = _pinned_index_digest()
    actual = _index_digest()
    assert pinned == actual, (
        "tests/unit/test_dashboard_comms_css.py's INDEX_HTML_SHA256 is stale: "
        f"pinned {pinned}, actual {actual}. Re-pin it to the digest of the "
        "edited static/index.html."
    )


def test_index_html_sha256_pin_has_a_repin_comment_above_it():
    """The re-pin must be documented with a short comment block directly
    above the pin, in the style of the existing re-pin notes."""
    guard = COMMS_CSS_GUARD.read_text(encoding="utf-8").splitlines()
    pin_line = next(
        (i for i, ln in enumerate(guard) if ln.startswith("INDEX_HTML_SHA256")),
        None,
    )
    assert pin_line is not None, "INDEX_HTML_SHA256 pin line not found"
    comment_block = []
    i = pin_line - 1
    while i >= 0 and guard[i].lstrip().startswith("#"):
        comment_block.append(guard[i])
        i -= 1
    assert comment_block, (
        "expected a comment block directly above INDEX_HTML_SHA256 describing "
        "the re-pin"
    )
    joined = "\n".join(comment_block).lower()
    assert "ingest" in joined, (
        "the comment block directly above INDEX_HTML_SHA256 must describe the "
        "chat-ingest hand-off re-pin; got:\n" + "\n".join(reversed(comment_block))
    )
