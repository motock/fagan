PROGRESS: 0/5

Task: Create `systemd/pipeline-logs.logrotate.template` with exact content.

Constraints:
- Four log paths, each containing literal `{{REPO_ROOT}}`.
- No `{{HOME}}` token.
- Must contain `copytruncate`.
- Must contain `rotate 5`.

Next step: create the file.
