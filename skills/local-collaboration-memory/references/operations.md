# Operations

All examples use a synthetic project. Run from the skill directory, using the same private home for every command. Use `--help` for the full CLI. Ordinary results are UTF-8 JSON.

## Store and retrieve

```sh
python -B scripts/entry.py --home ~/.local-collaboration-memory remember --kind project --title "Sensor validation" --content "Units checked against the source table. Next: verify the baseline." --scope project:sensor-demo --key current-state --source "Synthetic example: source-table review" --status confirmed
python -B scripts/entry.py --home ~/.local-collaboration-memory context --project sensor-demo --markdown
python -B scripts/entry.py --home ~/.local-collaboration-memory search "units" --scope project:sensor-demo
```

Do not run example writes against a user's established home merely to demonstrate the commands. `get <id>` reads revision metadata; `revise <id> --expected-revision <n> --source <actual-source> --content <correction>` creates a traceable correction. Suggestions use `--status candidate`; unsupported facts use `unverified`; fictional content uses `--kind persona` and is excluded by default.

## Project handoff

`project checkpoint <slug> --input <json-file>` accepts a JSON object shaped as follows (replace the root with a verified local directory and the evidence with an actual source):

```json
{
  "key": "overview",
  "expected_revision": 0,
  "data": {
    "root": "<absolute-project-directory>",
    "title": "Sensor demonstration",
    "goal": "Validate a sensor workflow",
    "current_state": "Source units have been checked",
    "next_steps": ["Verify baseline output"],
    "coverage": "Source review only",
    "artifacts": []
  },
  "source": "Actual review message or artifact",
  "status": "confirmed"
}
```

Use revision zero only when creating a new key. Later replacements supply the current revision and the full entry. Issue entries use stable keys and `data` fields `title`, `category`, `state`, `problem`, `solution`, `result`, `next_step` and project-relative artifacts. Confirm export success, then inspect `project show <slug>` and the two protected Markdown handoff files. Read before updating; do not overwrite manual notes.

## Optional session registry

`session update --input <json-file>` accepts `operation=ensure` with a host-verified native `thread_id`, `platform`, `host`, `title` and `source`. The independent registry allocates stable codes with decimal 00–99 suffixes and alphabetic rollover. `title-plan` only plans; `observe-title` requires an actual host readback. `directory-plan` plans a task path; `bind` requires an already verified project overview and current revision. It never moves an existing folder automatically. A separate daily-chat entry can be explicitly bound to an already registered session using `daily-chat bind --input <json-file>` with `thread_id`, `title`, `source` and `expected_revision`.

## Review-first retention

Configure only the user's chosen roots using `cleanup configure --root <workspace> --days 15`. `cleanup enroll <processing-directory>` opts a directory in. `cleanup activity <exact-path> --reason <actual-use>` records real use. `cleanup scan` discovers current candidate files and proposes a list; scanning or reminding does not count as use. `cleanup status` reads proposals.

Show the exact current proposal to the user. Only after explicit confirmation of that list may `cleanup decide <target-id> recycle --proposal-id <id> --file <relative-file> --reason <actual-confirmation-source> --confirmed` recycle selected eligible files. Respect protective flags, changed-file checks and manual notes. `cleanup restore <job-id>` restores a prior recycle job. Never substitute these commands for permission, infer consent from silence or auto-delete unknown files. A periodic reminder requires an independently authorized host scheduler; none is preinstalled.

## Backup, export and removal

`backup` snapshots memory SQLite. `session backup` snapshots the independent registry, including the daily-chat entry. Neither command includes the independent habits/proactive databases. `export` generates readable Markdown with the current 10,000-record projection bound. A full deployment also includes retention state/recycle contents, settings and integration backups; copy a quiescent private home for a complete migration, not just the Markdown projection.

`restore <backup-path>` changes memory state and first creates a safety snapshot. Test it in an isolated home, pause all other writers and retain the independent registry. `forget <id> --expected-revision <n>` removes the active revision-chain text from the live memory database; old backups, assistant history and previously exported files may still contain it and must be considered separately.

## Optional learning, preparation and code reuse

Read [optional features](optional-features.md) for opt-in policy examples, receipt-bound observations and safe code recycling. Code reuse is distinct from processing-record file recycling: the former archives registration generations, while the latter moves only explicitly confirmed files.
