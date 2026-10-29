"""The CI rollup: one GraphQL call for every open PR's check state.

``GitHubForge.checks_snapshot`` asks GitHub for ONE sha's check runs via the
REST ``commits/<sha>/check-runs`` endpoint, so its cost grows with the number
of open PRs the pipeline advances. This module asks a single GraphQL query for
the ``statusCheckRollup`` of every open PR of a repository, so the cost grows
with the number of repositories instead.

Two invariants hold for every reader of this module:

* Only the first :data:`_PR_PAGE_SIZE` open PRs are fetched. A sha that is
  beyond that window is simply absent from the rollup, and the caller falls
  back to the per-sha REST path for it - the rollup is an optimisation, never
  the only source of truth.
* The mapping is built from the SAME vocabulary ``checks_snapshot`` uses:
  check-run nodes are classified by
  :meth:`pipeline.forge_github.GitHubForge._classify_check_runs`, and
  commit-status contexts by the bucket states the adapter already knows. A
  value outside that vocabulary makes that one sha unreadable and omits it
  from the rollup, so the REST path decides for it; it never guesses a state
  and never discards the other shas.

Nothing here logs or raises with host output: a failure names the action and,
at most, the exception class, never stdout, stderr, a token, a payload or a
filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .forge import (
    CANCELLED,  # noqa: F401  (re-exported for callers mapping states)
    FAIL,
    PASS,
    PENDING,
    CISnapshot,
    CIStatusUnreadable,
)
from .forge_github import GitHubForge

_LOGGER = logging.getLogger(__name__)

# Bounded so a hung ``gh`` can never stall an advance tick.
_TIMEOUT_S = 20
# The open-PR window, and the context window inside one PR's rollup.
_PR_PAGE_SIZE = 100
_CONTEXT_PAGE_SIZE = 100

# The commit-status states GitHub reports for a ``StatusContext`` node,
# mapped through the vocabulary the forge adapter already uses for the legacy
# bucket values: ``error`` is a failure there, and an ``expected`` status (one
# GitHub says should exist but has not been created yet) is not a pass.
_STATUS_CONTEXT_STATES = {
    "SUCCESS": PASS,
    "FAILURE": FAIL,
    "ERROR": FAIL,
    "PENDING": PENDING,
    "EXPECTED": PENDING,
}

_PR_QUERY = f"""
query($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(states: OPEN, first: {_PR_PAGE_SIZE}) {{
      nodes {{
        headRefOid
        commits(last: 1) {{
          nodes {{
            commit {{
              statusCheckRollup {{
                contexts(first: {_CONTEXT_PAGE_SIZE}) {{
                  nodes {{
                    __typename
                    ... on CheckRun {{ name status conclusion }}
                    ... on StatusContext {{ context state }}
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

# repo_root -> (owner, name). Populated only after a successful parse, so one
# transient failure cannot poison every later tick.
_OWNER_CACHE: dict[str, tuple[str, str]] = {}


def _owner_and_name(repo_root: str) -> tuple[str, str]:
    """Resolve the repository's owner and name once per ``repo_root``."""
    cached = _OWNER_CACHE.get(repo_root)
    if cached is not None:
        return cached

    argv = ["gh", "repo", "view", "--json", "owner,name"]
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise CIStatusUnreadable(
            "gh repo view timed out resolving the repository owner and name"
        ) from None
    except OSError as e:
        raise CIStatusUnreadable(
            "gh unavailable resolving the repository owner and name: "
            f"{type(e).__name__}"
        ) from None

    if proc.returncode != 0:
        raise CIStatusUnreadable(
            "gh repo view failed resolving the repository owner and name"
        )

    try:
        payload = json.loads(proc.stdout or "")
    except ValueError:
        raise CIStatusUnreadable(
            "unparseable gh repo view owner/name output"
        ) from None

    if not isinstance(payload, dict):
        raise CIStatusUnreadable("gh repo view owner/name output is not an object")
    owner = payload.get("owner")
    name = payload.get("name")
    if isinstance(owner, dict):
        owner = owner.get("login")
    if not isinstance(owner, str) or not owner or not isinstance(name, str) or not name:
        raise CIStatusUnreadable("gh repo view owner/name output is incomplete")

    _OWNER_CACHE[repo_root] = (owner, name)
    return owner, name


def _graphql_payload(repo_root: str, owner: str, name: str) -> dict:
    """Run the one rollup query and return its parsed JSON payload."""
    argv = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_PR_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
    ]
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise CIStatusUnreadable("gh api graphql timed out fetching the CI rollup") from None
    except OSError as e:
        raise CIStatusUnreadable(
            f"gh unavailable fetching the CI rollup: {type(e).__name__}"
        ) from None

    if proc.returncode != 0:
        raise CIStatusUnreadable("gh api graphql failed fetching the CI rollup")

    try:
        payload = json.loads(proc.stdout or "")
    except ValueError:
        raise CIStatusUnreadable(
            "unparseable gh api graphql CI rollup output"
        ) from None

    if not isinstance(payload, dict):
        raise CIStatusUnreadable("gh api graphql CI rollup output is not an object")
    return payload


def _contexts_of(pr_node: dict) -> list:
    """The ``contexts.nodes`` list of one PR, tolerating a null rollup."""
    commits = pr_node.get("commits")
    if commits is None:
        return []
    if not isinstance(commits, dict):
        raise CIStatusUnreadable("CI rollup commit list has an unexpected shape")
    commit_nodes = commits.get("nodes")
    if commit_nodes is None:
        return []
    if not isinstance(commit_nodes, list):
        raise CIStatusUnreadable("CI rollup commit list has an unexpected shape")
    if not commit_nodes:
        return []
    first = commit_nodes[0]
    if not isinstance(first, dict):
        raise CIStatusUnreadable("CI rollup commit list has an unexpected shape")
    commit = first.get("commit")
    if not isinstance(commit, dict):
        raise CIStatusUnreadable("CI rollup commit has an unexpected shape")
    rollup = commit.get("statusCheckRollup")
    if rollup is None:
        # No CI on that sha: an empty snapshot is what ``checks_snapshot``
        # returns for a sha with no check runs, not an unreadable one.
        return []
    if not isinstance(rollup, dict):
        raise CIStatusUnreadable("CI rollup checks have an unexpected shape")
    contexts = rollup.get("contexts")
    if contexts is None:
        return []
    if not isinstance(contexts, dict):
        raise CIStatusUnreadable("CI rollup checks have an unexpected shape")
    nodes = contexts.get("nodes")
    if nodes is None:
        return []
    if not isinstance(nodes, list):
        raise CIStatusUnreadable("CI rollup checks have an unexpected shape")
    return nodes


def _lower(value):
    """Lower-case a GraphQL enum value, leaving ``None`` untouched."""
    return value.lower() if isinstance(value, str) else value


def _classify_contexts(nodes: list) -> CISnapshot:
    """Classify one sha's context nodes; raises when a value is unrecognised.

    The check-run half is delegated to the adapter's own
    :meth:`GitHubForge._classify_check_runs` so both readers can never
    disagree about one check.
    """
    check_lines: list[str] = []
    status_states: list[tuple[str, str]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise CIStatusUnreadable("CI rollup check has an unexpected shape")
        typename = node.get("__typename")
        if typename == "CheckRun":
            # GraphQL reports the enum in upper case (``COMPLETED``,
            # ``SUCCESS``); the REST payload ``checks_snapshot`` classifies
            # reports it in lower case. Normalise so both readers feed the
            # adapter the same line shape.
            check_lines.append(
                json.dumps(
                    {
                        "name": node.get("name"),
                        "status": _lower(node.get("status")),
                        "conclusion": _lower(node.get("conclusion")),
                    }
                )
            )
        elif typename == "StatusContext":
            name = node.get("context") or f"context-{index}"
            status_states.append((str(name), (node.get("state") or "")))
        else:
            # An unknown node kind cannot be classified without guessing.
            raise CIStatusUnreadable("unrecognised CI status node kind")

    snapshot = (
        GitHubForge._classify_check_runs("\n".join(check_lines))
        if check_lines
        else CISnapshot()
    )
    for name, raw_state in status_states:
        state = _STATUS_CONTEXT_STATES.get(str(raw_state).upper())
        if state is None:
            raise CIStatusUnreadable("unrecognised commit-status state")
        snapshot[name] = state
    return snapshot


def fetch_rollup(repo_root: str) -> dict[str, CISnapshot]:
    """Fetch ``{head sha: snapshot}`` for every open PR of ``repo_root``.

    Only the first :data:`_PR_PAGE_SIZE` open PRs are included; a sha beyond
    that window is absent and its caller falls back to the per-sha REST call.
    A PR whose checks fall outside the known vocabulary is omitted for the
    same reason. Raises :class:`CIStatusUnreadable` when the status cannot be
    read at all.
    """
    owner, name = _owner_and_name(repo_root)
    payload = _graphql_payload(repo_root, owner, name)

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CIStatusUnreadable("CI rollup response carries no data")
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise CIStatusUnreadable("CI rollup response carries no repository")
    pull_requests = repository.get("pullRequests")
    if not isinstance(pull_requests, dict):
        raise CIStatusUnreadable("CI rollup response carries no pull requests")
    pr_nodes = pull_requests.get("nodes")
    if pr_nodes is None:
        pr_nodes = []
    if not isinstance(pr_nodes, list):
        raise CIStatusUnreadable("CI rollup pull request list has an unexpected shape")

    rollup: dict[str, CISnapshot] = {}
    omitted = 0
    for pr_node in pr_nodes:
        if not isinstance(pr_node, dict):
            raise CIStatusUnreadable("CI rollup pull request has an unexpected shape")
        sha = pr_node.get("headRefOid")
        if not isinstance(sha, str) or not sha:
            # No head sha to key the snapshot by; the REST path is unaffected.
            omitted += 1
            continue
        try:
            snapshot = _classify_contexts(_contexts_of(pr_node))
        except CIStatusUnreadable:
            # One sha the rollup cannot classify must not cost the others
            # their fast path: leave it out so REST decides for it.
            omitted += 1
            continue
        rollup[sha] = snapshot

    if omitted:
        _LOGGER.debug(
            "CI rollup omitted pull requests with unclassifiable status",
            extra={"repo_owner": owner, "omitted": omitted, "fetched": len(rollup)},
        )
    return rollup


def _cache_path(repo_root: str, state_root: Path) -> Path:
    """``<state_root>/ci_rollup/<sha256(repo_root)[:16]>.json``."""
    digest = hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16]
    return Path(state_root) / "ci_rollup" / f"{digest}.json"


def store_rollup(
    repo_root: str, rollup: dict[str, CISnapshot], state_root: Path
) -> None:
    """Persist ``rollup`` for ``repo_root`` under ``state_root``.

    The directory is private (``0o700``) and the file is written atomically in
    that same directory with ``0o600``, so a concurrent reader sees either the
    previous file or the complete new one, never a partial write.
    """
    cache_dir = Path(state_root) / "ci_rollup"
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Tighten a directory that a previous version (or an operator) left open.
    os.chmod(cache_dir, 0o700)

    payload = {
        "fetched_at": time.time(),
        "shas": {sha: dict(snapshot) for sha, snapshot in rollup.items()},
    }
    target = _cache_path(repo_root, state_root)

    fd, tmp_name = tempfile.mkstemp(dir=str(cache_dir), prefix=".rollup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def cached_snapshot(
    repo_root: str, sha: str, state_root: Path, max_age_s: float
) -> CISnapshot | None:
    """The cached snapshot for ``sha``, or ``None`` when there is not one.

    A pure read: a missing, unreadable, aged or sha-less cache yields ``None``
    and never creates, chmods, deletes or rewrites anything. A stored sha with
    an empty check map is returned as an empty snapshot, exactly as
    ``checks_snapshot`` reports a sha whose checks have not registered yet.
    """
    try:
        raw = _cache_path(repo_root, state_root).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    fetched_at = payload.get("fetched_at")
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - float(fetched_at) > max_age_s:
        return None

    shas = payload.get("shas")
    if not isinstance(shas, dict):
        return None
    states = shas.get(sha)
    if not isinstance(states, dict):
        return None
    return CISnapshot({str(name): str(state) for name, state in states.items()})
