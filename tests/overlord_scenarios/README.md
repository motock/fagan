# Overlord hard-case scenarios

A scenario is a known-answer case for the overlord. Each one carries a question
exactly as the overlord receives it, the answers it may give, and the answers it
must never give. Before a policy, context or model change goes live, the
overlord is asked every scenario, and the change passes only when each answer
is one the scenario allows.

Scenarios cover cases whose right answer is known in advance, including cases
recorded history has not produced yet. Recorded history is checked separately by
`python -m pipeline.overlord_replay`.

## Schema

One JSON object per file. The file name, without `.json`, is the scenario `id`.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Equal to the file name without `.json`. Lowercase letters, digits and `-`. |
| `kind` | `"triage"` or `"merge"` | Which overlord question the case asks. |
| `description` | string | One line saying what the case tests. |
| `park_class` | string | Triage: a key of `pipeline.triage_park_classes.ALLOWED_ACTIONS`. Merge: `"high_risk_merge"`. |
| `question` | string | The question part of the prompt, as `pipeline.overlord_replay` rebuilds it. Triage: from a newline and `TRIAGE QUESTION:` to the end of the response format. Merge: the whole prompt, starting `MERGE ADJUDICATION:`. |
| `expected` | list of strings | Answers that pass. Triage: `ACTION` values. Merge: `proceed` or `park`. At least one. |
| `forbidden` | list of strings | Answers that fail the case if given. May be empty. |

`expected` and `forbidden` must not share a value. Triage values must be action
names in `EXISTING_ACTIONS` or `NEW_ACTIONS`.

Example (`merge-green-in-scope.json`, abridged):

```json
{
  "id": "merge-green-in-scope",
  "kind": "merge",
  "description": "Checks green, every file declared, no test edits; must merge.",
  "park_class": "high_risk_merge",
  "question": "MERGE ADJUDICATION: ...\nRespond in this exact format:\nRULING: proceed|park\nRATIONALE: <one line>\n",
  "expected": ["proceed"],
  "forbidden": []
}
```

## Secrets and customer data

The overlord only ever sees redacted text, so a scenario must match that. Write a
secret as `[redacted]`, never as a token-shaped string or a real credential. The
loader rejects any file that matches the secret patterns in
`pipeline.context_sources.scan_for_secrets`, and the error names the pattern
label, not the value.

Do not put real customer data, names, repository contents or internal paths in a
scenario. Keep such cases in your own repository.

## Running the check

```sh
.venv/bin/python -m pipeline.overlord_scenarios
```

With no options it runs the cases in this directory once each, using the shipped
policy. Useful options:

- `--scenarios DIR` (repeatable) runs the cases in `DIR` instead of this
  directory. It replaces the shipped set, so include this directory explicitly if
  you want both.
- `--policy FILE` runs triage prompts against a candidate policy.
- `--context DIR` adds context sources to every prompt, one per regular file, as
  `pipeline.overlord_replay --context` does.
- `--runs N` (1 to 5) asks each case N times. A case passes only if every answer
  passes.
- `--json` prints one object per case instead of a table.

Exit status is 0 when every case passes. It is 1 when a case fails, a file is
invalid, a scenarios directory is missing, or no cases are found. The check is
read-only and calls the overlord.

## Your own cases

Keep your own cases in your own repository and run them with
`--scenarios DIR`. Use the same schema, and name each file after its `id`.
