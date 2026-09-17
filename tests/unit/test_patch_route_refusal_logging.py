"""WAP-10 logging: refused patch review/apply requests log identifiers only.

The two WAP-10 dashboard routes (``GET /api/worktree/patch/{patch_id}`` and
``POST /api/worktree/patch/{patch_id}/apply``) refuse a lot of traffic: a
non-ui origin, an unknown/expired patch id, a wrong confirmation token, an
active story.  This story makes every one of those refusals observable in the
log -- and only in a way that cannot leak a credential.

The contract under test:

* ``app.dashboard._origin_class`` maps the caller-controlled
  ``X-Pipeline-Origin`` header to a closed vocabulary: ``absent`` (None),
  ``chat``, ``ui`` (exact, case-sensitive) and ``other`` for everything else.
  The raw header value is NEVER logged.
* An origin refusal logs one ``WARNING`` "Patch route origin refused" with
  ``route``, ``patch_id[:64]`` and ``origin_class``.
* An engine refusal logs "Patch apply refused" at ``WARNING`` for a 403 and
  ``INFO`` otherwise, with route/patch_id/plan_name/story_key/status_code/error.
* A missing (unknown or expired) patch logs ``INFO`` "Patch not found" with
  route and ``patch_id[:64]``.
* A successful GET or apply emits NO new record from ``app.dashboard``.
* Tokens, request bodies, diff content and raw header values never appear in a
  message or an ``extra`` field.

These tests are self-contained (helpers copied, not imported, from
``tests/unit/test_patch_apply_routes.py``) and must fail for the right reason
-- ``AttributeError`` on the missing ``_origin_class`` helper and zero log
records -- until the implementation lands.
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard_module
from app.auth import ORIGIN_CHAT, ORIGIN_HEADER, ORIGIN_UI, get_or_create_api_key
from pipeline import worktree_patch

PLAN_NAME = "wap10logplan"
STORY_KEY = "WAP-10"

GET_PATH = "/api/worktree/patch/{patch_id}"
APPLY_PATH = "/api/worktree/patch/{patch_id}/apply"

GENERIC_DETAIL = "origin not permitted"
NO_SUCH_PATCH_DETAIL = "no such patch"

LOGGER_NAME = "app.dashboard"

ORIGIN_REFUSED_MSG = "Patch route origin refused"
APPLY_REFUSED_MSG = "Patch apply refused"
NOT_FOUND_MSG = "Patch not found"

#: A small, valid one-file diff (one context line, one delete, one add).
VALID_DIFF = (
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " x = 1\n"
    "-y = 2\n"
    "+y = 3\n"
)

#: A distinctive token value that must never reach the log.
SUBMITTED_TOKEN = "SUBMITTED-TOKEN-DEADBEEF-1234"

REFERENCE_SECTION = "## Patch review & apply (WAP-9/WAP-10)"
REFERENCE_ANCHOR = "on any of these routes is `401` like anywhere else."
REFERENCE_PARAGRAPH = (
    "Refused review/apply requests are logged by `app.dashboard` with "
    "identifiers only -- route, truncated `patch_id`, an origin class "
    "(`absent`/`chat`/`ui`/`other`), and for engine refusals the plan, story, "
    "status code and error string. An origin refusal or a wrong token logs at "
    "`WARNING`; not-found and other refusals at `INFO`. Tokens, request "
    "bodies, diff content and raw header values are never logged."
)


# --------------------------------------------------------------------------
# fixtures / helpers (copied from tests/unit/test_patch_apply_routes.py)
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(dashboard_module.app)


@pytest.fixture(autouse=True)
def _clean_patch_store():
    """Keep the in-process record store from leaking between tests."""
    store = getattr(worktree_patch, "_PATCH_STORE", None)
    if isinstance(store, dict):
        store.clear()
    yield
    if isinstance(store, dict):
        store.clear()


def _auth_headers(**extra: str) -> dict:
    headers = {"X-Pipeline-Api-Key": get_or_create_api_key()}
    headers.update(extra)
    return headers


def _create_record(diff_text: str = VALID_DIFF, paths=None) -> dict:
    """Create a real pending record directly in the store; return its envelope."""
    return worktree_patch.create_patch_record(
        PLAN_NAME,
        STORY_KEY,
        diff_text,
        ["src/app.py"] if paths is None else paths,
        1,
    )


def _token_for(patch_id: str) -> str:
    return worktree_patch.confirmation_token_for(worktree_patch._PATCH_STORE[patch_id])


def _expire(patch_id: str) -> None:
    """Force a stored record's ``expires_at`` into the past."""
    from datetime import datetime, timedelta, timezone

    worktree_patch._PATCH_STORE[patch_id]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()


class _ApplySpy:
    """Records every ``apply_patch`` call and returns a canned result."""

    def __init__(self, result: dict | None = None):
        self.result = result if result is not None else {
            "ok": True,
            "patch_id": "wp-spy",
            "applied": ["src/app.py"],
        }
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def spy(monkeypatch):
    """Install a spy over the apply engine (no git is ever reached)."""
    s = _ApplySpy()
    monkeypatch.setattr(worktree_patch, "apply_patch", s)
    return s


def _get(client, patch_id, *, origin=None, key=None):
    headers = {}
    if key is not None:
        headers["X-Pipeline-Api-Key"] = key
    if origin is not None:
        headers[ORIGIN_HEADER] = origin
    return client.get(GET_PATH.format(patch_id=patch_id), headers=headers)


def _apply(client, patch_id, *, origin=None, key=None, body=None):
    headers = {}
    if key is not None:
        headers["X-Pipeline-Api-Key"] = key
    if origin is not None:
        headers[ORIGIN_HEADER] = origin
    return client.post(
        APPLY_PATH.format(patch_id=patch_id),
        json={} if body is None else body,
        headers=headers,
    )


def _records(caplog, min_level: int = logging.INFO) -> list:
    """Every captured record from ``app.dashboard`` at *min_level* or above."""
    return [
        r
        for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno >= min_level
    ]


def _blob(record) -> str:
    """Message plus every ``extra`` field of *record*, as one searchable string."""
    parts = [record.getMessage()]
    for key, value in record.__dict__.items():
        parts.append(f"{key}={value!r}")
    return "\n".join(parts)


def _all_blobs(caplog) -> str:
    return "\n".join(_blob(r) for r in caplog.records)


def _route_source(path: str, method: str) -> str:
    for route in dashboard_module.app.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            return inspect.getsource(route.endpoint)
    raise AssertionError(f"route {method} {path} is not registered")


# =========================================================================== #
# The _origin_class helper
# =========================================================================== #
class TestOriginClassHelper:
    def test_helper_exists_and_is_callable(self):
        helper = getattr(dashboard_module, "_origin_class", None)
        assert callable(helper), "app.dashboard._origin_class is missing"

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "absent"),
            ("chat", "chat"),
            ("ui", "ui"),
            ("", "other"),
            ("UI", "other"),
            ("Chat", "other"),
            ("other", "other"),
            (" chat", "other"),
            ("ui ", "other"),
            ("<script>evil</script>", "other"),
        ],
    )
    def test_mapping(self, value, expected):
        assert dashboard_module._origin_class(value) == expected

    def test_returns_a_str(self):
        assert isinstance(dashboard_module._origin_class(None), str)
        assert isinstance(dashboard_module._origin_class("ui"), str)

    def test_signature_takes_one_origin_argument(self):
        params = inspect.signature(dashboard_module._origin_class).parameters
        assert list(params) == ["x_pipeline_origin"]

    def test_is_a_module_level_function(self):
        assert inspect.isfunction(dashboard_module._origin_class)
        assert dashboard_module._origin_class.__module__ == "app.dashboard"


class TestHelperPlacement:
    def test_helper_is_defined_exactly_once(self):
        src = inspect.getsource(dashboard_module)
        assert src.count("def _origin_class") == 1

    def test_helper_sits_immediately_above_the_get_route(self):
        src = inspect.getsource(dashboard_module)
        helper_idx = src.index("def _origin_class")
        decorator_idx = src.index('@app.get("/api/worktree/patch/{patch_id}")')
        assert helper_idx < decorator_idx
        between = src[helper_idx:decorator_idx]
        assert between.count("def ") == 1, "another def sits between the helper and the route"
        assert "@app." not in between

    def test_no_second_module_logger_was_created(self):
        src = inspect.getsource(dashboard_module)
        assert src.count("logging.getLogger(") == 1


# =========================================================================== #
# Origin refusals
# =========================================================================== #
class TestOriginRefusalLogging:
    def test_apply_chat_origin_logs_one_warning(self, client, caplog, spy):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()
        patch_id = envelope["patch_id"]
        token = _token_for(patch_id)

        resp = _apply(
            client,
            patch_id,
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body={"confirmation_token": token},
        )

        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        rec = warnings[0]
        assert rec.levelno == logging.WARNING
        assert rec.getMessage() == ORIGIN_REFUSED_MSG
        assert rec.route == "apply"
        assert rec.origin_class == "chat"
        assert rec.patch_id == patch_id
        assert token not in caplog.text
        assert token not in _all_blobs(caplog)
        assert spy.calls == [], "the engine must not be reached on an origin refusal"

    def test_get_absent_origin_logs_one_warning(self, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()
        patch_id = envelope["patch_id"]

        resp = _get(client, patch_id, key=get_or_create_api_key())

        assert resp.status_code == 403
        assert resp.json()["detail"] == GENERIC_DETAIL
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        rec = warnings[0]
        assert rec.getMessage() == ORIGIN_REFUSED_MSG
        assert rec.route == "review"
        assert rec.origin_class == "absent"
        assert rec.patch_id == patch_id

    def test_unknown_origin_logs_other_and_hides_the_raw_value(self, client, caplog, spy):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()
        patch_id = envelope["patch_id"]

        resp = _apply(
            client,
            patch_id,
            origin="<script>evil</script>",
            key=get_or_create_api_key(),
            body={"confirmation_token": _token_for(patch_id)},
        )

        assert resp.status_code == 403
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert warnings[0].getMessage() == ORIGIN_REFUSED_MSG
        assert warnings[0].origin_class == "other"
        assert warnings[0].route == "apply"
        assert "evil" not in caplog.text
        assert "evil" not in _all_blobs(caplog)

    def test_get_unknown_origin_logs_other(self, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()

        resp = _get(
            client,
            envelope["patch_id"],
            origin="<script>evil</script>",
            key=get_or_create_api_key(),
        )

        assert resp.status_code == 403
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert warnings[0].route == "review"
        assert warnings[0].origin_class == "other"
        assert "evil" not in caplog.text

    def test_get_chat_origin_logs_warning(self, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()

        resp = _get(
            client,
            envelope["patch_id"],
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
        )

        assert resp.status_code == 403
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert warnings[0].getMessage() == ORIGIN_REFUSED_MSG
        assert warnings[0].route == "review"
        assert warnings[0].origin_class == "chat"

    def test_origin_gate_runs_before_the_record_lookup(self, client, caplog, spy):
        """A chat-origin request for an unknown id is refused as an origin
        refusal (403), never as a not-found (404)."""
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        resp = _apply(
            client,
            "wp-unknown",
            origin=ORIGIN_CHAT,
            key=get_or_create_api_key(),
            body={"confirmation_token": SUBMITTED_TOKEN},
        )

        assert resp.status_code == 403
        records = _records(caplog, logging.INFO)
        assert len(records) == 1
        assert records[0].getMessage() == ORIGIN_REFUSED_MSG
        assert records[0].route == "apply"
        assert records[0].origin_class == "chat"
        assert spy.calls == []


# =========================================================================== #
# Engine refusals on apply
# =========================================================================== #
class TestApplyEngineRefusalLogging:
    def test_wrong_token_403_logs_warning(self, client, caplog, spy):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        spy.result = {
            "ok": False,
            "error": "invalid confirmation token",
            "status_code": 403,
        }
        envelope = _create_record()
        patch_id = envelope["patch_id"]

        resp = _apply(
            client,
            patch_id,
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": SUBMITTED_TOKEN},
        )

        assert resp.status_code == 403
        assert resp.json()["detail"] == "invalid confirmation token"
        records = _records(caplog, logging.INFO)
        assert len(records) == 1, [r.getMessage() for r in records]
        rec = records[0]
        assert rec.levelno == logging.WARNING
        assert rec.getMessage() == APPLY_REFUSED_MSG
        assert rec.route == "apply"
        assert rec.patch_id == patch_id
        assert rec.plan_name == PLAN_NAME
        assert rec.story_key == STORY_KEY
        assert rec.status_code == 403
        assert rec.error == "invalid confirmation token"
        assert SUBMITTED_TOKEN not in caplog.text
        assert SUBMITTED_TOKEN not in _all_blobs(caplog)

    def test_active_story_409_logs_info_not_warning(self, client, caplog, spy):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        spy.result = {"ok": False, "error": "story is active", "status_code": 409}
        envelope = _create_record()
        patch_id = envelope["patch_id"]

        resp = _apply(
            client,
            patch_id,
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": _token_for(patch_id)},
        )

        assert resp.status_code == 409
        assert resp.json()["detail"] == "story is active"
        records = _records(caplog, logging.INFO)
        assert len(records) == 1, [r.getMessage() for r in records]
        rec = records[0]
        assert rec.levelno == logging.INFO
        assert rec.getMessage() == APPLY_REFUSED_MSG
        assert rec.status_code == 409
        assert rec.error == "story is active"
        assert rec.plan_name == PLAN_NAME
        assert rec.story_key == STORY_KEY
        assert _records(caplog, logging.WARNING) == []

    def test_engine_call_arguments_are_unchanged(self, client, caplog, spy):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        spy.result = {"ok": False, "error": "story is active", "status_code": 409}
        envelope = _create_record()
        patch_id = envelope["patch_id"]
        token = _token_for(patch_id)

        _apply(
            client,
            patch_id,
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": token},
        )

        assert len(spy.calls) == 1
        args, kwargs = spy.calls[0]
        assert args == (PLAN_NAME, STORY_KEY, patch_id, token)
        assert kwargs == {}


# =========================================================================== #
# Not-found (unknown / expired) logging
# =========================================================================== #
class TestNotFoundLogging:
    def test_apply_unknown_patch_logs_info(self, client, caplog, spy):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        resp = _apply(
            client,
            "wp-does-not-exist",
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": SUBMITTED_TOKEN},
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == NO_SUCH_PATCH_DETAIL
        records = _records(caplog, logging.INFO)
        assert len(records) == 1, [r.getMessage() for r in records]
        rec = records[0]
        assert rec.levelno == logging.INFO
        assert rec.getMessage() == NOT_FOUND_MSG
        assert rec.route == "apply"
        assert rec.patch_id == "wp-does-not-exist"
        assert spy.calls == []
        assert SUBMITTED_TOKEN not in _all_blobs(caplog)

    def test_get_unknown_patch_logs_info(self, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        resp = _get(
            client,
            "wp-does-not-exist",
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
        )

        assert resp.status_code == 404
        assert resp.json()["detail"] == NO_SUCH_PATCH_DETAIL
        records = _records(caplog, logging.INFO)
        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.INFO
        assert rec.getMessage() == NOT_FOUND_MSG
        assert rec.route == "review"
        assert rec.patch_id == "wp-does-not-exist"

    def test_expired_patch_logs_info(self, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()
        patch_id = envelope["patch_id"]
        _expire(patch_id)

        resp = _get(client, patch_id, origin=ORIGIN_UI, key=get_or_create_api_key())

        assert resp.status_code == 404
        records = _records(caplog, logging.INFO)
        assert len(records) == 1
        assert records[0].getMessage() == NOT_FOUND_MSG
        assert records[0].route == "review"

    def test_long_patch_id_is_truncated_to_64(self, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        long_id = "a" * 200

        resp = _get(client, long_id, origin=ORIGIN_UI, key=get_or_create_api_key())

        assert resp.status_code == 404
        records = _records(caplog, logging.INFO)
        assert len(records) == 1
        assert records[0].patch_id == "a" * 64
        assert len(records[0].patch_id) == 64


# =========================================================================== #
# Success emits nothing
# =========================================================================== #
class TestSuccessEmitsNoRecords:
    def test_successful_get_logs_nothing(self, client, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()
        patch_id = envelope["patch_id"]

        resp = _get(client, patch_id, origin=ORIGIN_UI, key=get_or_create_api_key())

        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.json()["diff_text"] == VALID_DIFF
        assert _records(caplog, logging.INFO) == []
        assert VALID_DIFF not in caplog.text

    def test_successful_apply_logs_nothing(self, client, caplog, spy):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        envelope = _create_record()
        patch_id = envelope["patch_id"]
        token = _token_for(patch_id)

        resp = _apply(
            client,
            patch_id,
            origin=ORIGIN_UI,
            key=get_or_create_api_key(),
            body={"confirmation_token": token},
        )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert _records(caplog, logging.INFO) == []
        assert VALID_DIFF not in caplog.text
        assert token not in _all_blobs(caplog)


# =========================================================================== #
# Source hygiene
# =========================================================================== #
class TestSourceHygiene:
    def test_no_confirmation_token_inside_a_logger_call(self):
        src = inspect.getsource(dashboard_module)
        offenders = [
            line
            for line in src.splitlines()
            if "logger." in line and "confirmation_token" in line
        ]
        assert offenders == []

    def test_no_diff_text_inside_a_logger_call(self):
        src = inspect.getsource(dashboard_module)
        offenders = [
            line for line in src.splitlines() if "logger." in line and "diff_text" in line
        ]
        assert offenders == []

    def test_refusal_logging_classifies_the_origin(self):
        src = inspect.getsource(dashboard_module)
        assert "_origin_class(x_pipeline_origin)" in src

    def test_require_ui_origin_literal_is_kept_in_both_routes(self):
        for path, method in ((GET_PATH, "GET"), (APPLY_PATH, "POST")):
            assert "require_ui_origin" in _route_source(path, method), path

    def test_origin_gate_is_wrapped_in_a_try_except(self):
        for path, method in ((GET_PATH, "GET"), (APPLY_PATH, "POST")):
            src = _route_source(path, method)
            assert "try:" in src, path
            assert "except HTTPException:" in src, path

    def test_apply_route_logs_before_raising_the_engine_refusal(self):
        src = _route_source(APPLY_PATH, "POST")
        assert APPLY_REFUSED_MSG in src, "the engine-refusal log call is missing"
        log_idx = src.index(APPLY_REFUSED_MSG)
        raise_idx = src.index("raise HTTPException(status_code=result")
        assert log_idx < raise_idx

    def test_bottom_of_file_sentinels_are_intact(self):
        src = inspect.getsource(dashboard_module)
        assert 'app.mount("/", StaticFiles(' in src
        non_empty = [line for line in src.splitlines() if line.strip()]
        assert non_empty[-1].strip() == "# End of file"

    def test_propose_route_origin_check_is_untouched(self):
        src = _route_source("/api/worktree/patch/propose", "POST")
        assert "x_pipeline_origin not in (ORIGIN_CHAT, ORIGIN_UI)" in src


# =========================================================================== #
# REFERENCE.md
# =========================================================================== #
class TestReferenceDoc:
    def _text(self) -> str:
        return Path("REFERENCE.md").read_text(encoding="utf-8")

    def _flat(self) -> str:
        """Whitespace-collapsed text: pins the wording, not the line wrapping."""
        import re

        return re.sub(r"\s+", " ", self._text())

    def test_paragraph_is_present(self):
        assert REFERENCE_PARAGRAPH in self._flat(), (
            "the WAP-10 logging paragraph is missing from REFERENCE.md"
        )

    def test_paragraph_follows_the_anchor_sentence(self):
        flat = self._flat()
        assert REFERENCE_ANCHOR in flat
        assert REFERENCE_PARAGRAPH in flat, "the WAP-10 logging paragraph is missing"
        anchor_end = flat.index(REFERENCE_ANCHOR) + len(REFERENCE_ANCHOR)
        para_idx = flat.index(REFERENCE_PARAGRAPH)
        assert para_idx > anchor_end
        assert flat[anchor_end:para_idx].strip() == ""

    def test_paragraph_sits_inside_the_wap_9_10_section(self):
        flat = self._flat()
        assert REFERENCE_SECTION in flat
        assert REFERENCE_PARAGRAPH in flat, "the WAP-10 logging paragraph is missing"
        assert flat.index(REFERENCE_SECTION) < flat.index(REFERENCE_PARAGRAPH)

    def test_paragraph_is_immediately_before_the_separator(self):
        flat = self._flat()
        assert REFERENCE_PARAGRAPH in flat, "the WAP-10 logging paragraph is missing"
        tail = flat[flat.index(REFERENCE_PARAGRAPH) + len(REFERENCE_PARAGRAPH):]
        assert tail.lstrip().startswith("---")
