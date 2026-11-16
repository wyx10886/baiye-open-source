# Setup

Python 3.11 or newer is required. There are no runtime dependencies or required external services. Keep this skill directory stable after host integration; installed configuration points at its absolute launcher path.

## Source / skill installation

Copy this entire `local-collaboration-memory` directory into a supported skill directory (for Codex, `~/.codex/skills/`). A workspace copy also works when invoked with its full path. Do not copy an existing private memory home with it.

From the skill directory, initialize a new private home:

```sh
python -B scripts/entry.py --home ~/.local-collaboration-memory init
python -B scripts/entry.py --home ~/.local-collaboration-memory stats
python -B scripts/entry.py --home ~/.local-collaboration-memory context --markdown
```

PowerShell accepts these arguments as well. Substitute a quoted, user-selected path when necessary. Every invocation must use the same home. `LOCAL_MEMORY_HOME` is the optional default-home environment variable; an explicit `--home` takes precedence. Initialization creates empty storage and does not import files or modify host configuration.

For a standalone local manager:

```sh
python -B scripts/entry.py --home ~/.local-collaboration-memory serve --open
```

The manager binds to loopback only and generates a private access token. Keep the generated URL/token private. All browser assets are bundled.

## MCP only

Configure a stdio server in the chosen host using this command shape:

```json
{
  "command": "<absolute-python-executable>",
  "args": ["<absolute-skill-path>/scripts/entry.py", "--home", "<absolute-private-home>", "mcp"]
}
```

These placeholders must be replaced locally. The server exposes 16 tools: `local_context`, `local_search`, `local_remember`, `local_revise`, `local_forget`, `local_review`, `local_stats`, `local_project_summary`, `local_project_checkpoint`, `local_session_lookup`, `local_session_update`, `local_daily_chat_lookup`, `local_daily_chat_bind`, `local_cleanup_scan`, `local_cleanup_activity` and `local_cleanup_decide`. Use `tools/list` for the complete schemas; MCP tools may be namespaced by the host. Merely configuring a server does not prove connection: initialize it, list tools and query an empty/new home before claiming activation. Read a single record with CLI `get` or inspect retrieved revision metadata.

## Optional full Codex integration

Only after the user requests lifecycle integration:

```sh
python -B scripts/entry.py --home ~/.local-collaboration-memory install --codex-home ~/.codex
python -B scripts/entry.py --home ~/.local-collaboration-memory doctor --codex-home ~/.codex
```

This backs up changed files in the private home, adds a managed MCP block, preserves/chains the prior notify command, adds SessionStart/UserPromptSubmit hooks and updates its own generated rules block in AGENTS.md. It does not replace unrelated rules. Reload the host/MCP as needed and review hook trust in the application; the installer never bypasses it. The generated fallback command contains local absolute paths and belongs only in private configuration.

Title synchronization is disabled by default. It can be explicitly enabled in the private home's `settings.json` using `host_title_sync.enabled=true`, a local `codex` binary path and the intended `codex_home`. Read `scripts/local_memory/title_sync.py` and verify host compatibility before opting in. The adapter uses the local app-server and cannot serve as a portable guarantee of title changes across hosts.

Automatic session numbering checks local host identity metadata even with title sync disabled. Configure the intended `host_title_sync.codex_home` when using a nondefault host home. The check reads the exact session's source metadata, skips child agents and defers unknown identities without allocating a code. See [privacy](privacy.md) for the read boundaries. An unavailable or incompatible host index does not prove a main session.

For an initial smoke test, use temporary memory and Codex homes so no live host settings are changed. `doctor` checks static integration/storage details; it does not establish scheduler health or actual future hook execution.

## Optional Python package

From this skill directory: `python -m pip install .`. This creates the `local-memory` CLI. The source launcher remains sufficient without installation. Building a wheel requires setuptools and wheel; they are build tools, not runtime dependencies. No PyPI publication is performed by this skill.
