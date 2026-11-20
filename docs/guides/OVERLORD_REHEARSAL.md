# Rehearsing full autonomy on a mirror

Before the overlord is allowed to act on a real repository, run full autonomy
against a throwaway copy. The rehearsal replays a plan that already finished
for real, and `python -m pipeline.rehearsal_report` shows, story by story, what
the overlord did differently from what actually happened.

> **Warning: the rehearsal spends model usage.** Every ruling, merge
> adjudication and triage action calls a model, exactly as a real run does.
> Replaying a 30-story plan costs about what that plan cost the first time.
> Set a spend limit on the rehearsal's model provider before you start.

The rehearsal never touches the real repository. Its stack has no push
credentials for it, and its git remote is a local mirror.

## Prerequisites

- Docker with the Compose plugin.
- A core checkout of this repository, on the commit you are testing.
- The stock standalone stack in `deploy/standalone` (see
  [DEPLOY_STANDALONE.md](../DEPLOY_STANDALONE.md)). Do not edit its compose file;
  the rehearsal overrides only what it must.
- Read access to the real repository, used once to create the mirror.
- The finished plan, exported from the real stack.

## 1. Create the mirrors

Pick the base commit the rehearsal starts from: the commit the plan's first
story was dispatched on. Call it `BASE`.

```bash
REHEARSAL=/srv/rehearsal            # any absolute directory outside the real checkout
mkdir -p "$REHEARSAL" && cd "$REHEARSAL"

# A bare mirror of the real repository. Read-only use of your credentials.
git clone --mirror https://git.example.invalid/org/repo.git mirror.git

# Cut the real remote off the mirror, so nothing in the stack can fetch from
# or push to the real repository through it.
git -C mirror.git remote remove origin

# The real default branch, kept under a name the report can use.
git -C mirror.git update-ref refs/heads/reference/main refs/heads/main

# The rehearsal's default branch starts at the base commit, not at the real tip.
git -C mirror.git update-ref refs/heads/main "$BASE"

# The rehearsal's working clone. Its origin is the local mirror only.
git clone mirror.git work
git -C work branch reference/main origin/reference/main
```

Check the result before going on:

```bash
git -C mirror.git rev-parse refs/heads/main          # prints: the base commit
git -C work rev-parse --abbrev-ref HEAD              # prints: main
git -C work rev-parse reference/main                 # the real default branch's tip
git -C work remote -v                                # origin points at mirror.git only
```

`main` in the mirror is the rehearsal's default branch, starting at the base.
`reference/main` holds what really happened. The report reads both.

## 2. Start a separate stack

Use its own compose project name, its own state volume and its own published
port. Two stacks on one host must not share any of these.

Write `REHEARSAL/override.yaml`. It mounts the mirrors into the server
container and sets the rehearsal's settings (step 3). The standalone compose
file sets its own `environment:` block and does not forward host variables, so
the settings must go in this file, not in your shell:

```yaml
services:
  pipeline:
    environment:
      PIPELINE_AUTONOMY: full
      PIPELINE_AUTO_TRIAGE: "1"
      PIPELINE_TRIAGE_MODE: execute
      PIPELINE_AUTHORITY_MATRIX: /srv/rehearsal/overlord-authority.json
      PIPELINE_AUTHORITY_TEAM_LAYER: /srv/rehearsal/team-authority.json
    volumes:
      - /srv/rehearsal:/srv/rehearsal:rw
```

Write the org and team authority layers described in step 3 into
`/srv/rehearsal/`, then bring the stack up on its own port:

```bash
PUBLIC_PORT=9443 docker compose -p fagan-rehearsal \
  -f deploy/standalone/compose.yaml \
  -f /srv/rehearsal/override.yaml \
  up -d --build
```

`-p fagan-rehearsal` keeps this project's containers, networks and the
`pipeline-state` volume apart from the real stack's. `PUBLIC_PORT` must be a
free host port, not the real stack's. Check the published ports with
`docker compose -p fagan-rehearsal ps`.

## 3. Settings

| Setting | Value for the rehearsal | Why |
|---|---|---|
| `PIPELINE_AUTONOMY` | `full` | The overlord adjudicates `risk: high` merges and runs the triage sweep. |
| `PIPELINE_AUTO_TRIAGE` | `1` | Turns the triage sweep on. |
| `PIPELINE_TRIAGE_MODE` | `execute` (`PIPELINE_TRIAGE_MODE=execute`) | Without it the actions are recorded in shadow and nothing is executed, so there is nothing to compare. |
| `PIPELINE_AUTHORITY_MATRIX` | `/srv/rehearsal/overlord-authority.json` | The org layer, copied from the shipped `overlord-authority.json` with the change below. |
| `PIPELINE_AUTHORITY_TEAM_LAYER` | `/srv/rehearsal/team-authority.json` | The team layer that sets the classes under test. |

**Org layer.** The shipped org layer locks `high_risk_merge.tier` (`delegation:
locked`, value `human`), so a team layer cannot change it. Copy the file to
`/srv/rehearsal/overlord-authority.json` and change only that rule:

```json
{
  "id": "high_risk_merge.tier",
  "class": "high_risk_merge",
  "type": "ordered_enum",
  "values": ["human", "auto+notify", "auto"],
  "value": "auto+notify",
  "delegation": "tighten_only",
  "bounds": {"loosest": "auto+notify"}
}
```

Make the same change for every other class under test. Its `delegation` must
allow a team to set it, and its `value` must be within `bounds.loosest`.

**Team layer.** `/srv/rehearsal/team-authority.json` names each class under
test. A team layer may only narrow what the org layer allows:

```json
{
  "version": 1,
  "layer": "team",
  "rules": [
    {"id": "high_risk_merge.tier", "value": "auto+notify"}
  ]
}
```

Before you ingest, confirm the resolved values the way the server reads them
(the module and call are the ones `pipeline.triage_readiness` uses):

```bash
docker compose -p fagan-rehearsal exec pipeline \
  python -c "from pipeline.authority_matrix import FileLayerSource as F; print(F().resolve().values)"
```

Every class under test must show `auto+notify` in the printed values. If one
still shows `human`, the report will show that class's actions held, not
executed.

**Plan settings.** A plan's own `triage_mode` setting applies over
`PIPELINE_TRIAGE_MODE` (`_triage_mode` in `pipeline/triage.py`). Open the
exported plan and remove any `triage_mode` that is not `execute`, or the
rehearsal runs in shadow whatever the environment says.

## 4. Export, ingest, approve, run

1. In the **real** stack, export the finished plan with the `export_plan` tool
   and save the JSON.
2. In the **rehearsal** stack, point the plan's repository at the mirror. Set
   `repo_url` to a URL the rehearsal container can clone, for example
   `file:///srv/rehearsal/mirror.git`. Confirm that first:
   `docker compose -p fagan-rehearsal exec pipeline git clone --bare /srv/rehearsal/mirror.git /tmp/probe`.
   If the stack refuses that URL, mirror the repository over a local HTTP
   server instead. Do not point it at the real remote.
3. Ingest the plan with `ingest_plan`, then approve it with `approve_plan`.
4. Let it run until every story is `done` or `parked`. Watch the decisions log
   and the notifications; the stack notifies as a real run does.

## 5. Read the report

The report reads the plan from the rehearsal stack's store, so run it inside
that stack's container, with the checkout mounted at the same path:

```bash
git -C /srv/rehearsal/work fetch origin     # bring the rehearsal's merges into the checkout

docker compose -p fagan-rehearsal \
  -f deploy/standalone/compose.yaml \
  -f /srv/rehearsal/override.yaml \
  exec pipeline python -m pipeline.rehearsal_report \
  --plan PLAN \
  --repo /srv/rehearsal/work \
  --reference reference/main \
  --rehearsal-branch origin/main
```

`--rehearsal-branch origin/main` names the rehearsal's merges as the fetched
remote branch. Without it, the report reads the checkout's local `main`, which
still holds the base commit. Add `--json` for the full record.

The report is read-only. It never writes the store or any git ref, and each
git call is capped at 30 seconds. A story whose git query fails shows
`git: unavailable` and the rest of the report still runs.

Read it in this order:

1. **Summary.** `merged_in_both` is where the rehearsal did what really
   happened. `only_reference` is a story the real run merged and the rehearsal
   did not: the overlord parked or held it. `only_rehearsal` is the more
   serious case: the rehearsal merged a story the real run did not. `neither`
   is a story with no merge in either history, and `git_unavailable` is a story
   the report could not check.
2. **Executed actions.** Every triage action the rehearsal really executed,
   by story. Compare each one with what the reference did for that story.
3. **Human holds.** A story with `human_hold` true was held for a person by a
   `park_for_human` ruling, or by a held high-risk merge. Each hold a person
   would not have made counts against the class, as in the readiness report.
4. **Files only in rehearsal.** A file the rehearsal changed and the reference
   did not is a change the overlord made that no person made.

Do not promote a class on this report alone. Its counts are a comparison, not
a readiness verdict. The promotion rule is in REFERENCE.md under "Promotion
rule (shadow to execute)".

## 6. Tear down

```bash
docker compose -p fagan-rehearsal \
  -f deploy/standalone/compose.yaml \
  -f /srv/rehearsal/override.yaml \
  down -v
```

`down -v` removes the rehearsal's state volume with its store, so no rehearsal
record survives into the real stack. Then delete what the rehearsal created on
the host:

```bash
rm -rf /srv/rehearsal/mirror.git /srv/rehearsal/work
rm -f /srv/rehearsal/overlord-authority.json /srv/rehearsal/team-authority.json /srv/rehearsal/override.yaml
```

The settings lived only in `override.yaml`, so no host shell variable needs
unsetting.
