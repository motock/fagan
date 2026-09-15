# Design: making Fagan installable without a manual git clone

**Status:** proposal, not yet built. Written to inform a decision, not to be
implemented as-is.

## Problem

Right now the only supported install path is:

```bash
git clone https://github.com/motock/fagan.git
cd fagan
scripts/install.sh
```

Package directories (MCP registries, `awesome-*` lists, Glama, PulseMCP,
Smithery) generally expect a one-line install (`pip install`, `pipx install`,
`uvx run`, or an `npx`-style command). Several of these directories won't
accept a listing without one. This is a real barrier to the "get eyes on the
project" goal, independent of code quality.

## Why this isn't just adding a `[project]` table

`pyproject.toml` currently has no `[project]` table at all — only
`[tool.pytest.ini_options]` and `[tool.ruff...]`. Adding one sounds like a
five-minute fix, but the codebase's data files are located by climbing from
`__file__`, which only resolves correctly when the code runs from inside a
git checkout. This pattern appears in **14 files**, not the 2–3 I originally
assumed:

| File | What it locates via `Path(__file__).resolve().parent.parent` |
|---|---|
| `pipeline/preflight.py` | `model_registry.json` (repo root) |
| `app/dashboard.py` | `static/` (dashboard frontend assets) |
| `app/backend_ollama.py` | `scripts/local_agent.py`, `scripts/local_agent_oracle.py` (subprocess targets) |
| `app/pipeline_mcp_server.py`, `pipeline/server.py` | repo root, for various defaults |
| `app/auth.py` | the `.dashboard_api_key` file location |
| `app/role_registry.py` | registry resolution |
| `pipeline/workspace.py` | workspace root-of-truth checks |
| `scripts/local_agent.py`, `scripts/local_agent_oracle.py`, `scripts/mlx_server_wrapper.py`, `scripts/mlx_server_supervisor.py`, `scripts/smoke_getting_started.py`, `scripts/reset_false_positive_tests_passed.py` | repo root, for various defaults |

None of this is a bug — it's a consistent, intentional convention ("the repo
root is always two levels above any module") that works well for a
clone-and-run tool. But it means a `pip install fagan` into `site-packages`
would silently break at least three things: the model registry would not be
found, the dashboard would serve no static assets, and local dispatch would
fail to launch its subprocess — none of which would be obvious from the
install succeeding.

By contrast, `AGENTS_DIR`, `OVERLORD_POLICY`, `PLAN_DIR`, and `WORKTREE_ROOT`
already default to `~/.claude/...` — home-relative, not repo-relative — so
those four are already install-agnostic today.

## Options

### A. Do nothing structural — ship a one-line *install script* instead of a package

Host `scripts/install.sh` (or a thin wrapper around it) and document:

```bash
curl -fsSL https://raw.githubusercontent.com/motock/fagan/master/scripts/remote-install.sh | bash
```

This clones the repo to a fixed location (e.g. `~/.fagan`) and runs the
existing `install.sh`. Zero changes to the 14 files above; zero new risk to
the path assumptions. This is the same pattern nvm, deno, ohmyzsh, and rustup
use, and it satisfies "one-line install" for a README even though it isn't
`pip install`. It does **not** satisfy directories that specifically require
a `pip`/`pipx`/`uvx` entry (some do; check before assuming this closes the
gap everywhere).

**Effort:** ~half a day (one script + a doc update). **Risk:** near zero —
touches no existing runtime path.

### B. Minimal packaging — `pip install` gets you the CLI, not a self-contained install

Add a `[project]` table and a console-script entry point (`fagan-mcp`,
`fagan-dashboard`), but keep the data-file resolution as-is and *require*
`FAGAN_REPO_ROOT` (or reuse the existing `REPO_ROOT` convention) to point at
a real checkout for `model_registry.json`, `static/`, and `scripts/`. In
other words: `pip install` gets the entry point onto `PATH`, but the tool
still expects a companion clone for its data files, and fails closed with a
clear error if it can't find one.

**Effort:** ~1–2 days. **Risk:** low — no change to the 14 files' resolution
logic, just an explicit required env var and a clearer error message when
it's missing. **Downside:** it's a half-measure — `pip install fagan` won't
actually be self-sufficient, which may read as broken to someone who expects
a normal Python package.

### C. Full restructure — genuinely self-contained package

Move every repo-root-relative data file so it resolves via
`importlib.resources` against the installed package instead of `__file__`
climbing:

1. Move `static/` under `app/static/` (or reference it via
   `importlib.resources.files("app") / "static"` without moving it, if a
   build backend can be configured to include it from its current location).
2. Ship `model_registry.json` as package data under `pipeline/`, resolved via
   `importlib.resources`, while keeping the existing
   `PIPELINE_MODEL_REGISTRY_PATH` / `model_registry.local.json` override
   mechanism working unchanged (a user's local override always wins).
3. Ship `scripts/local_agent.py` and `scripts/local_agent_oracle.py` as part
   of the installed distribution (package data or console-scripts of their
   own), and change `app/backend_ollama.py`'s two hardcoded paths to resolve
   via `importlib.resources` or `importlib.util.find_spec` instead of
   `__file__` climbing.
4. Repeat for the remaining 8 files in the table above, each with its own
   verification that the new resolution works both from a git checkout
   *and* from a `pip install`.
5. Add `[project]` with `dependencies` (from `requirements.txt`),
   `optional-dependencies` (dashboard, dev), and console-script entry
   points.
6. Add a CI job that does `pip install .` into a clean venv (outside the
   repo checkout) and runs a smoke test — this is the only way to actually
   prove the self-contained claim; a unit test that mocks
   `importlib.resources` would not catch a real packaging miss.

**Effort:** genuinely a small project — realistically 5–7 stories (one per
file-group above, plus the CI smoke-test story), not a single afternoon.
**Risk:** each individual resolution-site swap is small and mechanically
testable, but the CI smoke test is the one piece of evidence that actually
matters, and it can only be validated by really building and installing the
package outside the repo tree — a unit test cannot substitute for it.

## Recommendation

Given single-maintainer bandwidth and that **zero external users have found
the repo yet** (per the current traffic numbers), I'd sequence this as:

1. **Ship Option A now, cheaply**, so the README has a real one-liner before
   any promotion push. This doesn't block on anything else in this doc.
2. **Hold Option C until Phase 1/2 shows real interest** (stars, issues, or
   a directory explicitly rejecting the listing for lacking `pip install`).
   Building genuine `pip install` support for a tool nobody has asked to
   install yet is solving a problem that doesn't exist yet — the classic
   premature-generalization trap this project's own CLAUDE.md warns against
   ("No speculative features"). If and when it's warranted, do it as a
   proper pipeline plan (5–7 stories per the breakdown in Option C), each
   with its own acceptance fixture proving the resolution works both
   in-repo and pip-installed — not as a single "add packaging" story, which
   would violate the sizing rules for any dispatch tier given how many
   independent resolution sites are involved.
3. **Skip Option B** — it's real effort for a result (`pip install` that
   still needs a companion clone) that's more confusing than either
   endpoint, and doesn't actually satisfy the directories that require it.

## Open question for the maintainer

Is there a specific directory or channel already known to require `pip`
specifically (as opposed to accepting a `git clone` + script install)? If
not, Option A likely closes the practical gap for Phase 2 distribution, and
Option C can wait indefinitely.
