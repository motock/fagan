"""Tests for d9428976: the OpenAPI contract is a snapshot plus a breaking-change gate.

The dashboard's HTTP surface is a contract with two consumers that are easy to
break silently: the JavaScript under ``static/`` and any external caller. Until
this story the spec's ``info.version`` was FastAPI's placeholder ``0.1.0`` and
nothing pinned the surface, so a renamed path or a dropped response property
shipped as a routine refactor.

The contract graded here:

* ``app.api_contract.API_VERSION`` is the semantic version of the surface and
  equals the committed snapshot's ``info.version``;
* ``docs.api.openapi.json`` is byte-for-byte what ``export_spec()`` produces
  from the live app, so a route change shows up as a diff in review;
* ``scripts.api_contract.classify`` sorts every difference into ``breaking`` or
  ``additive``, and a breaking difference is only allowed behind a MAJOR bump;
* every ``/api/`` literal the browser code calls still resolves to a route in
  the live spec.

The version gate is deliberately graded against synthetic spec pairs as well as
the committed one: the live-vs-snapshot comparison is empty by construction,
so on its own it would pass even if the gate did nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app import api_contract as app_contract
from scripts import api_contract as contract
from scripts.api_contract import Change, classify, export_spec

_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT_PATH = _ROOT / "docs" / "api" / "openapi.json"
_STATIC_DIR = _ROOT / "static"

_SNAPSHOT_HINT = (
    "docs/api/openapi.json is out of date: run "
    "`python scripts/api_contract.py export` and commit the result. If the "
    "difference is a breaking change it needs a MAJOR bump of API_VERSION in "
    "app/api_contract.py."
)


# --------------------------------------------------------------------------- #
# Tiny spec builders: each test states only the shape it grades.
# --------------------------------------------------------------------------- #

def _spec(paths=None, version="1.0.0", schemas=None):
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": version},
        "paths": dict(paths or {}),
    }
    if schemas is not None:
        doc["components"] = {"schemas": schemas}
    return doc


def _operation(responses=None, parameters=None, request_schema=None, required=()):
    op = {"responses": dict(responses or {"200": {"description": "ok"}})}
    if parameters is not None:
        op["parameters"] = parameters
    if request_schema is not None:
        body = {"type": "object", "properties": dict(request_schema)}
        if required:
            body["required"] = list(required)
        op["requestBody"] = {"content": {"application/json": {"schema": body}}}
    return op


def _ok(properties, required=None):
    """A ``200`` response whose JSON body has the given properties."""
    schema = {"type": "object", "properties": dict(properties)}
    if required:
        schema["required"] = list(required)
    return {"200": {"description": "ok", "content": {"application/json": {"schema": schema}}}}


def _kinds(changes):
    return sorted(change.kind for change in changes)


def _where(changes):
    return " | ".join(change.where for change in changes)


def _major(version: str) -> int:
    return int(version.split(".", 1)[0])


def _gate_violations(snapshot, live):
    """Breaking changes that the version bump does not license.

    A breaking difference is allowed only when the live MAJOR is greater than
    the snapshot MAJOR; a MINOR or PATCH bump never licenses one.
    """
    breaking = [change for change in classify(snapshot, live) if change.kind == "breaking"]
    if not breaking:
        return []
    if _major(live["info"]["version"]) > _major(snapshot["info"]["version"]):
        return []
    return breaking


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #

def test_export_spec_matches_the_committed_snapshot():
    assert _SNAPSHOT_PATH.exists(), _SNAPSHOT_HINT

    committed = _SNAPSHOT_PATH.read_text(encoding="utf-8")

    assert export_spec() == committed, _SNAPSHOT_HINT


def test_export_spec_ends_with_a_newline_and_is_stable():
    first = export_spec()

    assert first.endswith("\n")
    assert export_spec() == first


def test_snapshot_is_serialised_with_sorted_keys_and_one_space_indent():
    committed = _SNAPSHOT_PATH.read_text(encoding="utf-8")
    parsed = json.loads(committed)

    assert committed == json.dumps(parsed, indent=1, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# The version gate
# --------------------------------------------------------------------------- #

def test_committed_snapshot_passes_the_version_gate():
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    live = json.loads(export_spec())

    assert _gate_violations(snapshot, live) == []


def test_removed_path_without_a_major_bump_fails_the_gate():
    snapshot = _spec({"/api/users": {"get": _operation()}}, version="1.0.0")
    live = _spec({}, version="1.0.0")

    violations = _gate_violations(snapshot, live)

    assert [change.kind for change in violations] == ["breaking"]
    assert "/api/users" in violations[0].where


def test_removed_path_with_a_major_bump_passes_the_gate():
    snapshot = _spec({"/api/users": {"get": _operation()}}, version="1.0.0")
    live = _spec({}, version="2.0.0")

    assert _gate_violations(snapshot, live) == []


def test_minor_bump_does_not_license_a_breaking_change():
    snapshot = _spec({"/api/users": {"get": _operation()}}, version="1.0.0")
    live = _spec({}, version="1.1.0")

    assert _gate_violations(snapshot, live), "1.0.0 -> 1.1.0 must not license a removal"


def test_major_bump_from_one_nine_nine_passes_the_gate():
    snapshot = _spec({"/api/users": {"get": _operation()}}, version="1.9.9")
    live = _spec({}, version="2.0.0")

    assert _gate_violations(snapshot, live) == []


def test_additive_only_change_needs_no_major_bump():
    snapshot = _spec({}, version="1.0.0")
    live = _spec({"/api/users": {"get": _operation()}}, version="1.0.0")

    assert _gate_violations(snapshot, live) == []


# --------------------------------------------------------------------------- #
# The classifier: paths and methods
# --------------------------------------------------------------------------- #

def test_removed_path_is_breaking():
    old = _spec({"/api/users": {"get": _operation()}})
    new = _spec({})

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "/api/users" in changes[0].where
    assert isinstance(changes[0], Change)


def test_removed_method_is_breaking():
    old = _spec({"/api/users": {"get": _operation(), "post": _operation()}})
    new = _spec({"/api/users": {"get": _operation()}})

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "post" in changes[0].where


def test_added_path_is_additive():
    old = _spec({})
    new = _spec({"/api/users": {"get": _operation()}})

    changes = classify(old, new)

    assert _kinds(changes) == ["additive"]
    assert "/api/users" in changes[0].where


def test_added_method_is_additive():
    old = _spec({"/api/users": {"get": _operation()}})
    new = _spec({"/api/users": {"get": _operation(), "delete": _operation()}})

    changes = classify(old, new)

    assert _kinds(changes) == ["additive"]
    assert "delete" in changes[0].where


def test_removed_response_status_is_breaking():
    old = _spec({
        "/api/users": {
            "get": _operation({
                "200": {"description": "ok"},
                "404": {"description": "missing"},
            })
        }
    })
    new = _spec({"/api/users": {"get": _operation({"200": {"description": "ok"}})}})

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "404" in changes[0].where


def test_removed_request_parameter_is_breaking():
    old = _spec({
        "/api/users": {
            "get": _operation(parameters=[
                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
            ])
        }
    })
    new = _spec({"/api/users": {"get": _operation()}})

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "limit" in changes[0].where


# --------------------------------------------------------------------------- #
# The classifier: response schemas
# --------------------------------------------------------------------------- #

def test_removed_response_property_is_breaking():
    old = _spec({
        "/api/users": {
            "get": _operation(_ok({
                "id": {"type": "string"},
                "display_name": {"type": "string"},
            }))
        }
    })
    new = _spec({"/api/users": {"get": _operation(_ok({"id": {"type": "string"}}))}})

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "display_name" in changes[0].where


def test_added_response_property_is_additive():
    old = _spec({"/api/users": {"get": _operation(_ok({"id": {"type": "string"}}))}})
    new = _spec({
        "/api/users": {
            "get": _operation(_ok({
                "id": {"type": "string"},
                "display_name": {"type": "string"},
            }))
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["additive"]
    assert "display_name" in changes[0].where


def test_response_property_type_change_is_breaking():
    old = _spec({"/api/users": {"get": _operation(_ok({"id": {"type": "string"}}))}})
    new = _spec({"/api/users": {"get": _operation(_ok({"id": {"type": "integer"}}))}})

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "id" in changes[0].where
    assert "string" in changes[0].what and "integer" in changes[0].what


def test_property_type_change_through_a_ref_is_breaking():
    old = _spec(
        {"/api/users": {"get": _operation(_ok({"id": {"$ref": "#/components/schemas/Id"}}))}},
        schemas={"Id": {"type": "string"}},
    )
    new = _spec(
        {"/api/users": {"get": _operation(_ok({"id": {"$ref": "#/components/schemas/Id"}}))}},
        schemas={"Id": {"type": "integer"}},
    )

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]


def test_removed_enum_value_is_breaking():
    old = _spec({
        "/api/users": {
            "get": _operation(_ok({"state": {"type": "string", "enum": ["queued", "running"]}}))
        }
    })
    new = _spec({
        "/api/users": {
            "get": _operation(_ok({"state": {"type": "string", "enum": ["queued"]}}))
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "running" in changes[0].what


def test_added_enum_value_is_additive():
    old = _spec({
        "/api/users": {
            "get": _operation(_ok({"state": {"type": "string", "enum": ["queued"]}}))
        }
    })
    new = _spec({
        "/api/users": {
            "get": _operation(_ok({"state": {"type": "string", "enum": ["queued", "running"]}}))
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["additive"]
    assert "running" in changes[0].what


# --------------------------------------------------------------------------- #
# The classifier: request schemas
# --------------------------------------------------------------------------- #

def test_new_required_request_parameter_is_breaking():
    old = _spec({"/api/users": {"get": _operation()}})
    new = _spec({
        "/api/users": {
            "get": _operation(parameters=[
                {"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}},
            ])
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "tenant" in changes[0].where


def test_new_optional_parameter_is_additive():
    old = _spec({"/api/users": {"get": _operation()}})
    new = _spec({
        "/api/users": {
            "get": _operation(parameters=[
                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
            ])
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["additive"]
    assert "limit" in changes[0].where


def test_parameter_becoming_required_is_breaking():
    old = _spec({
        "/api/users": {
            "get": _operation(parameters=[
                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
            ])
        }
    })
    new = _spec({
        "/api/users": {
            "get": _operation(parameters=[
                {
                    "name": "limit",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "integer"},
                },
            ])
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "limit" in changes[0].where


def test_new_required_request_body_property_is_breaking():
    old = _spec({
        "/api/users": {"post": _operation(request_schema={"name": {"type": "string"}})}
    })
    new = _spec({
        "/api/users": {
            "post": _operation(
                request_schema={"name": {"type": "string"}, "tenant": {"type": "string"}},
                required=["tenant"],
            )
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "tenant" in changes[0].where


def test_new_optional_request_body_property_is_additive():
    old = _spec({
        "/api/users": {"post": _operation(request_schema={"name": {"type": "string"}})}
    })
    new = _spec({
        "/api/users": {
            "post": _operation(
                request_schema={"name": {"type": "string"}, "note": {"type": "string"}}
            )
        }
    })

    changes = classify(old, new)

    assert _kinds(changes) == ["additive"]
    assert "note" in changes[0].where


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #

def test_check_accepts_the_committed_snapshot():
    assert contract.main(["check"]) == 0


def test_check_reports_a_breaking_change_without_rewriting_the_snapshot(
    tmp_path, monkeypatch, capsys
):
    snapshot = _spec({"/api/users": {"get": _operation()}})
    target = tmp_path / "openapi.json"
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    before = target.read_bytes()
    monkeypatch.setattr(contract, "SNAPSHOT_PATH", target)

    assert contract.main(["check"]) == 1
    printed = capsys.readouterr().out
    assert "breaking: /api/users" in printed
    # `check` must compare only: an implicit export would make the second run
    # green and quietly bless the change.
    assert target.read_bytes() == before
    assert contract.main(["check"]) == 1
    assert target.read_bytes() == before


# --------------------------------------------------------------------------- #
# The classifier: identity, cycles and refs
# --------------------------------------------------------------------------- #

def test_identical_specs_produce_no_changes():
    spec = _spec({"/api/users": {"get": _operation(_ok({"id": {"type": "string"}}))}})

    assert classify(spec, spec) == []
    assert classify(json.loads(json.dumps(spec)), spec) == []


def test_self_referential_schema_does_not_hang_and_is_repeatable():
    schemas = {
        "Node": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "child": {"$ref": "#/components/schemas/Node"},
            },
        }
    }
    old = _spec(
        {"/api/tree": {"get": _operation(_ok({"root": {"$ref": "#/components/schemas/Node"}}))}},
        schemas=schemas,
    )
    new = json.loads(json.dumps(old))

    assert classify(old, new) == []
    # A module-level "visited" set would make the second call see everything as
    # already handled (or as a cycle) and silently report nothing; per-call
    # state keeps the answer stable.
    assert classify(old, new) == []


def test_self_referential_schema_still_reports_a_removed_property():
    schemas = {
        "Node": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "child": {"$ref": "#/components/schemas/Node"},
            },
        }
    }
    old = _spec(
        {"/api/tree": {"get": _operation(_ok({"root": {"$ref": "#/components/schemas/Node"}}))}},
        schemas=schemas,
    )
    new = _spec(
        {"/api/tree": {"get": _operation(_ok({"root": {"$ref": "#/components/schemas/Node"}}))}},
        schemas={
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/components/schemas/Node"}},
            }
        },
    )

    changes = classify(old, new)

    assert _kinds(changes) == ["breaking"]
    assert "id" in changes[0].where


# --------------------------------------------------------------------------- #
# Client conformance: every /api/ literal in the browser code has a route
# --------------------------------------------------------------------------- #

_JS_LITERAL = re.compile(r"""(?P<quote>['"`])(?P<path>/api/[^'"`\n]*)(?P=quote)""")
_TEMPLATE_PARAM = re.compile(r"\$\{[^}]*\}")
_SPEC_PARAM = re.compile(r"\{[^/{}]*\}")


def _normalise(path: str) -> str:
    """Drop the query string/fragment and collapse template holes to a param."""
    path = path.split("#", 1)[0].split("?", 1)[0]
    return _TEMPLATE_PARAM.sub("{param}", path)


def _strip_js_comments(source: str) -> str:
    """Drop ``//`` and ``/* */`` comments, leaving string literals intact.

    Prose comments mention routes too (``main.js`` names the patch URLs in a
    note); those are documentation, not calls, and must not be graded as if the
    browser fetched them. The scanner tracks the enclosing quote so a ``//``
    inside a string (a URL scheme) is not mistaken for a comment.
    """
    out: list[str] = []
    index = 0
    quote = ""
    length = len(source)
    while index < length:
        char = source[index]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
            out.append(char)
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _extract_paths(source: str) -> set[str]:
    return {
        _normalise(match.group("path"))
        for match in _JS_LITERAL.finditer(_strip_js_comments(source))
    }


def _segments(path: str) -> list[str]:
    return path.split("/")


def _is_param(segment: str) -> bool:
    return bool(_SPEC_PARAM.fullmatch(segment))


def _path_matches(path: str, template: str) -> bool:
    """Does a client path satisfy a spec template, segment by segment?

    Segment-wise (never a wildcard across slashes) so a client path with the
    wrong number of segments cannot match by accident. Either side being a
    parameter makes that one segment free: the client's interpolation is
    unknown at build time, and the spec's parameter name is not part of the
    contract.
    """
    client = _segments(path)
    declared = _segments(template)
    if len(client) != len(declared):
        return False
    return all(
        _is_param(left) or _is_param(right) or left == right
        for left, right in zip(client, declared)
    )


def _unmatched(paths, templates) -> list[str]:
    return sorted(
        path
        for path in paths
        if not any(_path_matches(path, template) for template in templates)
    )


def _spec_templates():
    spec = json.loads(export_spec())
    return sorted(spec["paths"])


def _client_paths() -> set[str]:
    sources = sorted(_STATIC_DIR.rglob("*.js"))
    assert sources, f"no JavaScript found under {_STATIC_DIR}"
    paths: set[str] = set()
    for source in sources:
        paths.update(_extract_paths(source.read_text(encoding="utf-8")))
    return paths


def test_every_browser_api_call_matches_a_route_in_the_live_spec():
    unmatched = _unmatched(_client_paths(), _spec_templates())

    assert unmatched == [], (
        "the browser calls paths the live spec does not declare: "
        + ", ".join(unmatched)
    )


def test_the_extractor_finds_at_least_twenty_distinct_paths():
    # Without this floor the conformance test above would pass vacuously if the
    # extractor stopped matching.
    assert len(_client_paths()) >= 20


def test_extractor_strips_query_strings_and_collapses_template_holes():
    paths = _extract_paths("fetch('/api/users/${id}?active=1')")

    assert paths == {"/api/users/{param}"}
    assert _unmatched(paths, ["/api/users/{user_id}"]) == []


def test_unknown_browser_path_is_reported_as_unmatched():
    paths = _extract_paths("fetch('/api/not-a-route')")

    assert paths == {"/api/not-a-route"}
    assert _unmatched(paths, _spec_templates()) == ["/api/not-a-route"]


# --------------------------------------------------------------------------- #
# The version string itself
# --------------------------------------------------------------------------- #

def test_api_version_matches_the_snapshot_info_version():
    snapshotted = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))["info"]["version"]

    assert app_contract.API_VERSION == snapshotted


def test_api_version_is_semantic():
    assert re.fullmatch(r"\d+\.\d+\.\d+", app_contract.API_VERSION)


def test_the_served_spec_reports_the_contract_version():
    # The version is passed where the app is built, not through docs_kwargs():
    # docs_kwargs() is pinned exactly by test_dashboard_openapi_auth.py, so the
    # only thing worth grading here is what a caller can read off the served
    # document.
    served = json.loads(export_spec())

    assert served["info"]["version"] == app_contract.API_VERSION
    assert app_contract.docs_kwargs({app_contract.PUBLIC_DOCS_ENV: "1"}) == {}
    assert app_contract.docs_kwargs({})["openapi_url"] == app_contract.SPEC_PATH
