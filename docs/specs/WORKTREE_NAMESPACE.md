# Worktree Namespace (`PIPELINE_WORKTREE_NAMESPACE`)

## 1. What it does

`PIPELINE_WORKTREE_NAMESPACE` is an **opt-in** environment variable that inserts a
namespace directory between `WORKTREE_ROOT` and the story key:

| `PIPELINE_WORKTREE_NAMESPACE` | Worktree path |
|---|---|
| unset | `WORKTREE_ROOT/<story_key>` |
| `""` (empty) | `WORKTREE_ROOT/<story_key>` |
| `alice` | `WORKTREE_ROOT/alice/<story_key>` |

It is the directory-name counterpart of `PIPELINE_BRANCH_NAMESPACE`
(`pipeline/branch_names.py`), which namespaces the git ref as
`agent/<ns>/<key>`. Both are resolved by the same module so the two conventions
stay in one place.

Implementation: `pipeline/branch_names.py` defines
`WORKTREE_NAMESPACE_ENV = "PIPELINE_WORKTREE_NAMESPACE"` and
`worktree_dir_name(story_key) -> str`; `pipeline/dispatch.py` derives the
worktree path as `WORKTREE_ROOT / worktree_dir_name(story_key)`.

## 2. Default and behaviour preservation

With the variable unset (or empty) the result is the story key unchanged, so
every path is exactly `WORKTREE_ROOT/<story_key>` as before. This is the
default: nothing changes for an existing installation until the variable is
set.

## 3. Format and validation

The value must match the same pattern the branch namespace uses:

```
^[a-z0-9][a-z0-9-]{0,31}$
```

i.e. 1–32 characters, lowercase ASCII letters, digits and `-`, not starting
with `-`. A non-empty value that does not match raises `ValueError` naming
`PIPELINE_WORKTREE_NAMESPACE`. Unset and empty are always valid and mean "no
namespace".

The story key is **not** lowercased. `story_branch()` lowercases because git
refs are case-insensitive on macOS; directory names are not, and dispatch must
keep using the exact key spelling it has always used.

## 4. Operational constraint: set it before dispatching

Set `PIPELINE_WORKTREE_NAMESPACE` **before** dispatching stories, and do not
change it while any story is in flight.

The worktree path is derived from the story key and the *current* namespace at
the time it is computed. Unlike the branch name — which is recorded on the
story as `story["branch"]` at dispatch and reused on resume — the namespace is
not recorded anywhere. Changing the variable mid-flight therefore makes an
in-flight story resolve to a different directory: dispatch will not find the
existing worktree, will treat the story as a fresh dispatch, and will build a
second worktree (and, for the branch, still reuse the recorded ref). The old
directory is left behind, orphaned.

To change namespaces safely: let in-flight stories finish (or interrupt and
clean them up), then change the variable, then dispatch again.

## 5. How it complements `PIPELINE_BRANCH_NAMESPACE`

The two variables are independent and may be set separately, but they are
normally set together so that a per-operator namespace covers both the git ref
and the on-disk directory:

```
PIPELINE_BRANCH_NAMESPACE=alice      # refs:      agent/alice/<key>
PIPELINE_WORKTREE_NAMESPACE=alice    # worktrees: WORKTREE_ROOT/alice/<key>
```

Why both are needed: the branch namespace prevents two operators from pushing
to the same ref, while the worktree namespace prevents two operators on the
same machine (or a shared `WORKTREE_ROOT`) from colliding on the same
directory. The branch name is recorded on the story, so it survives a
namespace change; the worktree path is not, hence the constraint in section 4.

## 6. Collision detection

`pipeline/dispatch.py` still passes `worktree_path.parent` to
`cross_plan.key_conflicts`. With no namespace that is `WORKTREE_ROOT` — exactly
as before. With a namespace it is `WORKTREE_ROOT/<ns>`, which is the correct
root for same-namespace collisions: two plans sharing a key under the same
namespace still collide, while different namespaces do not.

## 7. Tests

`tests/unit/test_worktree_namespace.py` covers the unset/empty/set cases, key
case preservation, invalid values (uppercase, leading `-`, 33 characters,
embedded `/`, space, `_`), the 32-character boundary, the dispatch path
derivation with and without a namespace, and the `key_conflicts` root argument.
`tests/unit/test_branch_namespace.py` is unchanged and still passes.
