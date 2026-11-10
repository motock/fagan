"""`findings_are_parseable` - a REQUEST_CHANGES body that a rework agent can act on.

b4 evidence (2026-11-09/10, standalone stack): at 09:26:33 a reviewer
returned 1,631 characters of feedback that ended mid-sentence at "ONE blocking
finding remains", with the finding itself cut off. The reviewer persona emits
the VERDICT line FIRST and the findings AFTER it, so a length-truncated
response still parses as a verdict. The old `_has_review_findings` check only
asked "is there any non-VERDICT text at all?", so the cut-off response was
accepted as a genuine rejection and routed to a rework round - with no
`review_feedback` recorded, because the findings could not be extracted.

`findings_are_parseable(text)` answers the narrower question: can a rework
agent actually be handed this body? True when the body carries at least one
file-scoped Blocking finding OR an unscoped `- Blocking:` finding. False
otherwise.

The companion threshold lives in this module too: a body longer than the
truncation threshold (600 characters) whose findings are NOT parseable is
"unfocused/truncated" and must be treated as an inconclusive review rather
than a rejection. Terse stubs (e.g. "still bad\\nVERDICT: REQUEST_CHANGES")
stay below the threshold and keep today's behaviour.
"""

import pytest

from pipeline import parsers


def _parseable(text):
    """Call the new predicate.

    The lookup happens at CALL time so this module still imports while the
    implementation is missing - the failure is then a plain AttributeError
    on the predicate, not a collection error that hides every other test.
    """
    return parsers.findings_are_parseable(text)


def _finding_files(text):
    """Same, for the existing extractor."""
    return parsers._extract_blocking_finding_files(text)


# A long body with real, actionable findings: a file-scoped Blocking finding.
LONG_WITH_FILE_FINDING = (
    "The reviewer walked the diff and found two problems.\n"
    "- Blocking: pipeline/parsers.py: the parser drops the last line of the\n"
    "  response when the body has no trailing newline, so a truncated\n"
    "  review is indistinguishable from a complete one.\n"
    "The second problem is cosmetic and does not need to be fixed now.\n"
    "VERDICT: REQUEST_CHANGES\n"
)

# A long body whose only finding is unscoped - no file path to hand a rework
# agent, but it IS a real finding, so it stays parseable.
LONG_WITH_UNSCOPED_FINDING = (
    "Several paragraphs of review prose that say a lot without naming a "
    "single file. " * 12
    + "- Blocking: (unscoped) the retry loop swallows the timeout error.\n"
    "VERDICT: REQUEST_CHANGES\n"
)

# A long body with NO Blocking finding at all - the b4 shape: lots of prose
# that ends mid-sentence, with the actual finding cut off.
LONG_WITHOUT_FINDING = (
    "I read through the change carefully and there is a lot to unpack here. "
    * 12
    + "The retry path in the reviewer client needs to be reworked, and the "
    "error handling around it is not covered by any test in the suite. ONE "
    "blocking finding remains, and it concerns the way the client treats a "
    "partial resp"
)


def test_long_body_without_blocking_finding_is_not_parseable():
    """The b4 shape: prose that ends mid-sentence with no Blocking finding."""
    assert _parseable(LONG_WITHOUT_FINDING) is False


def test_long_body_with_file_scoped_blocking_finding_is_parseable():
    """A file-scoped Blocking finding is actionable -> parseable."""
    assert _parseable(LONG_WITH_FILE_FINDING) is True


def test_long_body_with_unscoped_blocking_finding_is_parseable():
    """An unscoped `- Blocking: (unscoped)` finding counts as a finding."""
    assert _parseable(LONG_WITH_UNSCOPED_FINDING) is True


def test_parseable_agrees_with_the_blocking_file_extraction():
    """A file-scoped Blocking finding is exactly what the extractor finds."""
    assert _finding_files(LONG_WITH_FILE_FINDING) == ["pipeline/parsers.py"]
    assert _parseable(LONG_WITH_FILE_FINDING) is True
    # The unscoped finding names no file, so the extractor finds nothing -
    # the unscoped arm of the predicate is what keeps it parseable.
    assert _finding_files(LONG_WITH_UNSCOPED_FINDING) == []
    assert _parseable(LONG_WITH_UNSCOPED_FINDING) is True


def test_empty_and_whitespace_bodies_are_not_parseable():
    """Boundary: nothing to act on is not parseable."""
    assert _parseable("") is False
    assert _parseable("   \n\t  ") is False


def test_verdict_only_body_is_not_parseable():
    """A bare VERDICT line carries no finding."""
    assert _parseable("VERDICT: REQUEST_CHANGES") is False


def test_terse_stub_body_is_not_parseable():
    """The reviewer-stub convention keeps working: terse prose, no finding."""
    assert _parseable("still bad\nVERDICT: REQUEST_CHANGES") is False


def test_blocking_finding_without_the_dash_is_parseable():
    """`Blocking:` and `- Blocking:` both name a finding."""
    body = "Some prose.\nBlocking: pipeline/x.py: it is wrong.\n"
    assert _parseable(body) is True


def test_blocking_word_without_a_finding_marker_is_not_parseable():
    """The word "blocking" in prose is not a finding."""
    body = "This is a blocking issue with the design of the whole thing. " * 20
    assert _parseable(body) is False


@pytest.mark.parametrize(
    "body",
    [
        "- Blocking: (unscoped) something is wrong",
        "prose\n- Blocking: (unscoped) something is wrong",
    ],
)
def test_unscoped_blocking_finding_is_parseable(body):
    """The `- Blocking: (unscoped)` marker is parseable on its own."""
    assert _parseable(body) is True


@pytest.mark.parametrize(
    "body",
    [
        # Prose that uses the word but never the marker.
        "The blocking part of this change is untested.\n",
        # A marker that is not a finding at all.
        "Blocking\n",
        # Only the verdict, with trailing whitespace.
        "VERDICT: REQUEST_CHANGES   \n",
    ],
)
def test_bodies_without_a_blocking_marker_are_not_parseable(body):
    """A body with no Blocking marker names no finding a rework can act on."""
    assert _parseable(body) is False
