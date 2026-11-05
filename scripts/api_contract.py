"""Snapshot and grade the dashboard's OpenAPI contract.

``docs/api/openapi.json`` is the committed snapshot of every route and schema
the dashboard serves. ``export`` rewrites it from the live application and
``check`` compares the live application against it, so CI fails when a route
change was not reflected in the snapshot:

    python scripts/api_contract.py export   # regenerate the snapshot
    python scripts/api_contract.py check    # exit 1 on any difference

``classify`` sorts each difference into ``breaking`` or ``additive``:

* BREAKING — a removed path or method, a removed response status, a property
  removed from a 2xx response schema, a property whose ``type`` changed, an
  enum value removed from a response, a request parameter or request-body
  property that became required or was added as required, and a request
  parameter the old spec declared that the new one dropped;
* ADDITIVE — everything else (new path, new optional parameter, new response
  property, new enum value, ...).

A breaking difference may only ship behind a MAJOR bump of
``app.api_contract.API_VERSION`` (the ``info.version`` of the snapshot); the
version gate lives in ``tests/unit/test_api_contract.py``.

LIMITS OF THE CLASSIFIER — it is a structural diff, not an OpenAPI type
checker. It resolves local ``$ref``s into ``components/schemas`` and compares
the resolved nodes as plain JSON; it does not model ``oneOf``/``allOf``/
``anyOf`` semantics at all, does not understand discriminators, formats,
defaults, pattern/format constraints, or nullable/optionality expressed through
union types, and it treats external (non-``#/``) references as opaque. Bodies
of non-2xx responses are compared leniently: a removed property there is
reported as additive. Differences it cannot see are exactly the ones that need
a human reader of the diff — the snapshot diff is the safety net, the
classifier is the gate.

The snapshot is taken in a NEUTRAL ENVIRONMENT: ``PIPELINE_*`` and
``LOCAL_AGENT_*`` are dropped while the application is imported, because some
of them (notably ``PIPELINE_IDENTITY_PROVIDER``/``PIPELINE_OIDC_*``) add or
remove routes. Without that, the same commit would export different bytes on a
developer's machine and in CI. Routes that exist only under a particular
operator configuration — the OIDC ``/auth/*`` redirects — are therefore not in
the snapshot; the contract covers the surface every deployment serves.

Standard library only; the application is imported lazily inside
``export_spec`` so importing this module never drags in FastAPI.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The committed snapshot, relative to the repository root.
SNAPSHOT_PATH = REPO_ROOT / "docs" / "api" / "openapi.json"

BREAKING = "breaking"
ADDITIVE = "additive"

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

#: A ``$ref`` chain longer than this is treated as a cycle rather than followed.
_MAX_REF_HOPS = 32

#: Environment prefixes that change which routes exist; see ``_neutral_environ``.
_ISOLATED_PREFIXES = ("PIPELINE_", "LOCAL_AGENT_")

#: Direction of a schema comparison: responses must not lose anything, requests
#: may not gain obligations, and anything outside a 2xx body is graded leniently.
_RESPONSE = "response"
_REQUEST = "request"
_LENIENT = "lenient"


@dataclass(frozen=True)
class Change:
    """One compatibility-relevant difference between two documents."""

    kind: str  #: ``BREAKING`` or ``ADDITIVE``
    where: str  #: the route/schema location, e.g. ``/api/plans GET responses.200.name``
    what: str  #: what changed, e.g. ``property removed``

    def __str__(self) -> str:
        return f"{self.kind}: {self.where}: {self.what}"


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

@contextmanager
def _neutral_environ() -> Iterator[None]:
    """Run the block with the route-changing environment variables removed.

    Restores the exact previous values afterwards, including which keys were
    simply absent.
    """
    saved = {
        key: os.environ[key]
        for key in list(os.environ)
        if key.startswith(_ISOLATED_PREFIXES)
    }
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(_ISOLATED_PREFIXES):
                del os.environ[key]
        os.environ.update(saved)


def _live_spec() -> dict[str, Any]:
    """The spec FastAPI generates for a neutral, default deployment.

    Imported here, not at module scope: this module is also imported by tests
    and tooling that have no business starting the dashboard. Two details are
    load-bearing for determinism:

    * the repository root is put on ``sys.path`` first, because running this
      file as ``python scripts/api_contract.py`` puts ``scripts/`` — not the
      root — there;
    * ``app.dashboard`` is re-executed inside ``_neutral_environ`` so a
      previously imported module (built with, say, OIDC configured, which
      registers extra ``/auth/*`` routes) cannot leak into the snapshot. The
      caller's module object is put back afterwards, so importing this
      function never changes what anyone else sees.
    """
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    with _neutral_environ():
        package = sys.modules.get("app")
        previous_attribute = getattr(package, "dashboard", None)
        previous_module = sys.modules.pop("app.dashboard", None)
        try:
            module = importlib.import_module("app.dashboard")
            return module.app.openapi()
        finally:
            if package is not None:
                if previous_attribute is not None:
                    package.dashboard = previous_attribute
                else:
                    delattr(package, "dashboard")
            if previous_module is not None:
                sys.modules["app.dashboard"] = previous_module
            else:
                sys.modules.pop("app.dashboard", None)


def export_spec() -> str:
    """The live spec as the exact bytes of the committed snapshot.

    Sorted keys and a one-space indent keep the diff of an unchanged surface
    empty; the trailing newline keeps the file POSIX-clean.
    """
    return json.dumps(_live_spec(), indent=1, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# Reference resolution
# --------------------------------------------------------------------------- #

def _lookup(document: Any, ref: str) -> Any:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _ref_of(node: Any) -> str | None:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"]
    return None


def _resolve(node: Any, document: Any) -> Any:
    """Follow local ``$ref``s; ``None`` when the chain is broken or cyclic."""
    seen: set[str] = set()
    hops = 0
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if ref in seen or hops >= _MAX_REF_HOPS:
            return None
        seen.add(ref)
        hops += 1
        node = _lookup(document, ref)
        if node is None:
            return None
    return node


# --------------------------------------------------------------------------- #
# Schema comparison
# --------------------------------------------------------------------------- #

def _removed_property_kind(direction: str) -> str:
    return BREAKING if direction == _RESPONSE else ADDITIVE


def _walk_schema(
    old_node: Any,
    new_node: Any,
    old_document: Any,
    new_document: Any,
    where: str,
    out: list[Change],
    active: frozenset[tuple[str | None, str | None]],
    direction: str,
) -> None:
    old = _resolve(old_node, old_document)
    new = _resolve(new_node, new_document)
    if not isinstance(old, dict) or not isinstance(new, dict):
        return

    key = (_ref_of(old_node), _ref_of(new_node))
    if key != (None, None):
        if key in active:
            # A reference cycle: the pair is already being compared higher up,
            # so one pass over it is the whole truth.
            return
        active = active | {key}

    old_type = old.get("type")
    new_type = new.get("type")
    if isinstance(old_type, str) and isinstance(new_type, str) and old_type != new_type:
        out.append(
            Change(BREAKING, where, f"type changed from {old_type!r} to {new_type!r}")
        )

    _compare_enums(old, new, where, out, direction)

    old_props = old.get("properties")
    new_props = new.get("properties")
    old_props = old_props if isinstance(old_props, dict) else {}
    new_props = new_props if isinstance(new_props, dict) else {}
    new_required = set(new.get("required") or ())
    old_required = set(old.get("required") or ())

    for name in sorted(old_props):
        if name not in new_props:
            out.append(
                Change(
                    _removed_property_kind(direction),
                    f"{where}.{name}",
                    "property removed",
                )
            )
            continue
        _walk_schema(
            old_props[name],
            new_props[name],
            old_document,
            new_document,
            f"{where}.{name}",
            out,
            active,
            direction,
        )

    for name in sorted(set(new_props) - set(old_props)):
        if direction == _REQUEST and name in new_required:
            out.append(Change(BREAKING, f"{where}.{name}", "required property added"))
        else:
            out.append(Change(ADDITIVE, f"{where}.{name}", "property added"))

    if direction == _REQUEST:
        for name in sorted(new_required - old_required):
            if name in old_props:
                out.append(Change(BREAKING, f"{where}.{name}", "property became required"))

    if isinstance(old.get("items"), dict) and isinstance(new.get("items"), dict):
        _walk_schema(
            old["items"],
            new["items"],
            old_document,
            new_document,
            f"{where}[]",
            out,
            active,
            direction,
        )


def _compare_enums(
    old: dict[str, Any],
    new: dict[str, Any],
    where: str,
    out: list[Change],
    direction: str,
) -> None:
    old_values = old.get("enum")
    new_values = new.get("enum")
    if not isinstance(old_values, list) or not isinstance(new_values, list):
        return
    for value in old_values:
        if value in new_values:
            continue
        kind = BREAKING if direction == _RESPONSE else ADDITIVE
        out.append(Change(kind, where, f"enum value removed: {value!r}"))
    for value in new_values:
        if value not in old_values:
            out.append(Change(ADDITIVE, where, f"enum value added: {value!r}"))


# --------------------------------------------------------------------------- #
# Operation comparison
# --------------------------------------------------------------------------- #

def _content_schema(container: Any, document: Any) -> Any:
    """The JSON schema of a request body or response, refs resolved."""
    if not isinstance(container, dict):
        return None
    content = container.get("content")
    if not isinstance(content, dict) or not content:
        return None
    media = content.get("application/json")
    if not isinstance(media, dict):
        media = next((item for item in content.values() if isinstance(item, dict)), None)
    if not isinstance(media, dict):
        return None
    return _resolve(media.get("schema"), document)


def _compare_response(
    old_response: Any,
    new_response: Any,
    old_document: Any,
    new_document: Any,
    status: str,
    where: str,
    out: list[Change],
) -> None:
    old_schema = _content_schema(old_response, old_document)
    new_schema = _content_schema(new_response, new_document)
    if old_schema is None or new_schema is None:
        return
    direction = _RESPONSE if status.startswith("2") else _LENIENT
    _walk_schema(
        old_schema,
        new_schema,
        old_document,
        new_document,
        f"{where} responses.{status}",
        out,
        frozenset(),
        direction,
    )


def _parameters(operation: Any) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    parameters = operation.get("parameters") if isinstance(operation, dict) else None
    for parameter in parameters or ():
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        if isinstance(name, str):
            result[(name, str(parameter.get("in")))] = parameter
    return result


def _compare_operation(
    old_operation: Any,
    new_operation: Any,
    old_document: Any,
    new_document: Any,
    where: str,
    out: list[Change],
) -> None:
    old_operation = old_operation if isinstance(old_operation, dict) else {}
    new_operation = new_operation if isinstance(new_operation, dict) else {}

    old_responses = old_operation.get("responses")
    new_responses = new_operation.get("responses")
    old_responses = old_responses if isinstance(old_responses, dict) else {}
    new_responses = new_responses if isinstance(new_responses, dict) else {}
    for status in sorted(old_responses, key=str):
        if status not in new_responses:
            out.append(
                Change(BREAKING, f"{where} responses.{status}", "response removed")
            )
            continue
        _compare_response(
            old_responses[status],
            new_responses[status],
            old_document,
            new_document,
            str(status),
            where,
            out,
        )
    for status in sorted(set(new_responses) - set(old_responses), key=str):
        out.append(Change(ADDITIVE, f"{where} responses.{status}", "response added"))

    old_parameters = _parameters(old_operation)
    new_parameters = _parameters(new_operation)
    for name, location in sorted(old_parameters):
        if (name, location) not in new_parameters:
            out.append(
                Change(
                    BREAKING,
                    f"{where} parameter {name}",
                    "request parameter removed",
                )
            )
            continue
        was_required = bool(old_parameters[(name, location)].get("required"))
        now_required = bool(new_parameters[(name, location)].get("required"))
        if now_required and not was_required:
            out.append(Change(BREAKING, f"{where} parameter {name}", "became required"))
    for name, location in sorted(set(new_parameters) - set(old_parameters)):
        if bool(new_parameters[(name, location)].get("required")):
            out.append(
                Change(
                    BREAKING,
                    f"{where} parameter {name}",
                    "required request parameter added",
                )
            )
        else:
            out.append(
                Change(
                    ADDITIVE,
                    f"{where} parameter {name}",
                    "optional request parameter added",
                )
            )

    old_body = _content_schema(old_operation.get("requestBody"), old_document)
    new_body = _content_schema(new_operation.get("requestBody"), new_document)
    if old_body is not None and new_body is not None:
        _walk_schema(
            old_body,
            new_body,
            old_document,
            new_document,
            f"{where} requestBody",
            out,
            frozenset(),
            _REQUEST,
        )


def _compare_path_item(
    path: str,
    old_item: Any,
    new_item: Any,
    old_document: Any,
    new_document: Any,
    out: list[Change],
) -> None:
    old_item = old_item if isinstance(old_item, dict) else {}
    new_item = new_item if isinstance(new_item, dict) else {}
    for method in _METHODS:
        in_old = method in old_item
        in_new = method in new_item
        if not in_old and not in_new:
            continue
        where = f"{path} {method}"
        if in_old and not in_new:
            out.append(Change(BREAKING, where, "method removed"))
        elif in_new and not in_old:
            out.append(Change(ADDITIVE, where, "method added"))
        else:
            _compare_operation(
                old_item[method],
                new_item[method],
                old_document,
                new_document,
                where,
                out,
            )


def classify(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """Every difference between two documents, graded ``breaking``/``additive``.

    ``old`` is the baseline (the snapshot), ``new`` the candidate. All state is
    per-call, so repeated calls on the same objects give the same answer.
    """
    out: list[Change] = []
    old_paths = old.get("paths") if isinstance(old.get("paths"), dict) else {}
    new_paths = new.get("paths") if isinstance(new.get("paths"), dict) else {}
    for path in sorted(old_paths):
        if path not in new_paths:
            out.append(Change(BREAKING, path, "path removed"))
            continue
        _compare_path_item(path, old_paths[path], new_paths[path], old, new, out)
    for path in sorted(set(new_paths) - set(old_paths)):
        out.append(Change(ADDITIVE, path, "path added"))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _command_export(_args: argparse.Namespace) -> int:
    text = export_spec()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH}")
    return 0


def _command_check(_args: argparse.Namespace) -> int:
    """Compare only — never export first, so a stale snapshot stays stale."""
    if not SNAPSHOT_PATH.exists():
        print(f"missing snapshot {SNAPSHOT_PATH}: run `export`", file=sys.stderr)
        return 1
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    live = json.loads(export_spec())
    changes = classify(snapshot, live)
    if not changes:
        print(f"contract matches {SNAPSHOT_PATH}")
        return 0
    for change in changes:
        print(change)
    print(
        "a breaking change needs a MAJOR bump of app.api_contract.API_VERSION "
        "plus `python scripts/api_contract.py export`",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="api_contract",
        description="Snapshot and grade the dashboard's OpenAPI contract.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    export = subcommands.add_parser(
        "export", help="write docs/api/openapi.json from the live app"
    )
    export.set_defaults(handler=_command_export)
    check = subcommands.add_parser(
        "check", help="exit 1 when the live spec differs from the snapshot"
    )
    check.set_defaults(handler=_command_check)
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
