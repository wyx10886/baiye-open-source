# Local Collaboration Memory

A self-contained agent skill for evidence-backed memory, project handoffs and traceable corrections. Python 3.11+, standard-library runtime, local SQLite, CLI, stdio MCP and a loopback web manager. Public skill version: **1.0.1**.

The download contains code and synthetic fixtures. Initialize your own empty private home outside this folder; no personal memory or machine configuration is included.

## Quick start

Copy this directory into your host's skill directory (Codex: `~/.codex/skills/local-collaboration-memory/`) or invoke it from a stable workspace path. From this directory:

```sh
python -B scripts/entry.py --home ~/.local-collaboration-memory init
python -B scripts/entry.py --home ~/.local-collaboration-memory context --markdown
python -B scripts/entry.py --home ~/.local-collaboration-memory serve --open
```

Invoke `$local-collaboration-memory` to set up or use the skill. See [SKILL.md](SKILL.md), [setup](references/setup.md), [operations](references/operations.md) and [privacy](references/privacy.md).

Full host lifecycle integration is optional and changes configuration/hooks/notify. Host title synchronization defaults to disabled. Review-first retention requires explicit workspace enrollment and confirmation of a concrete file list. No periodic job is preinstalled.

Version 1.0.1 verifies local host session metadata before automatic session numbering, skips child agents and leaves unknown identities pending. This read-only check also runs when title synchronization is disabled; see the privacy reference for its exact scope.

## Verify the source

```sh
python -B scripts/audit_release.py --manifest release-manifest.json
cd scripts
python -B -m unittest discover -s tests -v
python -B evaluation.py
```

Tests use temporary homes and synthetic records. Optional package installation: `python -m pip install .` from this directory, providing the `local-memory` command. This repository does not publish a PyPI package.

Current limitations: lexical retrieval; bounded context/Markdown projection; memory backup and session-registry backup are separate; `doctor` checks integration files rather than future scheduler execution. Selected context sent to a cloud assistant is subject to the host's data flow. See the privacy reference before enabling capture or sharing artifacts.

MIT licensed. Contributions should use synthetic fixtures and pass the release manifest/privacy audit.
