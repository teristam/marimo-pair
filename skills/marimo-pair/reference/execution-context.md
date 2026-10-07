# Connection Troubleshooting

Use this reference when `execute-code.sh` or MCP cannot reach the intended
marimo session, cannot select a session, or fails because code was passed
incorrectly.

## Targeting

- Pass the discovered `--url` unchanged; `null` means no address was reachable.
- With one open notebook, the script resolves its session.
- Use `--file` for an exact, stable file key or path. It resolves the current
  session on every call.
- Use `--session` for an exact active session ID; it goes stale on reconnect.
- The selectors are mutually exclusive. Preserve supplied values; never guess.

## Auth

For token-authenticated servers, set `MARIMO_TOKEN` in the user- or
harness-approved environment, then invoke the script without an inline
assignment (which may not match an allowed command pattern):

```bash
bash scripts/execute-code.sh --url http://localhost:2718 -c "1 + 1"
```

`--token` also works, but exposes the token in process arguments. If both are
present, `--token` overrides `MARIMO_TOKEN`. `execute-code.sh` places its
bearer header in `curl`'s argv, so using the environment avoids the token in
the script command itself but does not keep it out of every process listing.
The script sends it in the `Authorization` header on session discovery and
execution requests.

## Code Input

Pass code with `-c CODE`, `-` for stdin, or a file path.

```bash
bash scripts/execute-code.sh --url http://localhost:2718 - <<'PY'
print(df.head())
PY
```

```bash
bash scripts/execute-code.sh --url http://localhost:2718 /tmp/code.py
```

## Common Script Errors

- **`[]` from discover-servers.sh** - nothing is registered. Only servers
  started with `--no-token` register; otherwise ask the user for the URL, or
  start marimo with the project's normal tooling.
- **No active sessions on the server** - open the notebook in the browser.
- **No active session matches `--file`** - check the exact file key against the
  available paths printed by the script.
- **Multiple sessions on server** - several notebooks are open; rerun with the
  `--file` path or `--session` ID shown for the notebook you want.
- **Failed to connect** - check the URL, token, and whether the server is still
  running.
- **Execution did not complete** - the server ended the stream without a
  result. With `--file`, retry the same command so it resolves the current
  session. With `--session`, the ID is probably stale after a page refresh.
- **... is running on the Windows host but answered at no address reachable
  from WSL** - WSL's network cannot reach it. The message lists the fixes; the
  usual one is restarting marimo on the host with `--host 0.0.0.0`. Running the
  scripts from Git Bash or PowerShell on the Windows side also works.
- **needs jq / curl on PATH** - install them in whichever environment runs the
  scripts. Inside WSL that means the distro, not Windows.
- **SyntaxError** - the submitted Python was malformed; use a heredoc or file.
- **ImportError** - diagnose in the notebook kernel. Install packages through
  `cm` when needed.

## Branch-and-Explore Workers

This optional coordinator workflow leaves ordinary pairing on
`execute-code.sh` and server discovery. A branch worker is not selected through
discovery; it has a worker ID, loopback URL, session, token, temporary notebook,
and kernel. Branching requires a token-protected parent; a `--no-token` parent
is rejected. The coordinator gets the parent URL, token, and current session ID
from the user or an authenticated `/api/sessions` listing via separately
approved coordinator HTTP tooling. Select the session entry matching the
notebook's canonical path. `execute-code.sh` does not print the ID for a lone
session. After reconnect, query the authenticated listing again for the current
ID before `add`; never guess or reuse a stale ID.

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py start \
  --parent-url http://127.0.0.1:2718 --parent-session "$PARENT_SESSION" \
  --names records latest_model
```

Before this, the coordinator must run and await the required parent `help(cm)`
command using `execute-code.sh` with explicit `--url` and `--session`. The
worker is only handed off after startup creates and validates its session,
runs its own first code-mode inspection, and restores all requested names.
Use `python3` if needed. Set `MARIMO_TOKEN` in the coordinator environment
through user- or harness-approved secret injection before these commands; the
commands begin with an allowed `bash`, `python`, or `python3` form, while an
environment-assignment prefix may need explicit harness approval. Do not use
`--token`, and do not propagate the parent token into the branch-agent
environment. Environment variables are inherited by children. The parent is
ordinary edit-mode, not globally read-only; keep it idle during export because
pickle hooks may run user code. Workers do not replay preprocessing or copy
the parent graph.

### Role boundary and commands

The coordinator owns parent credentials and runs `start`, `list`, `add`, and
`stop`. It hands each branch agent only a worker-specific capability to run
code for the selected worker, for example:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py run --worker WORKER_ID - <<'PY'
print(records[:3])
PY
```

The run command accepts source only on stdin and routes by worker ID to the
recorded worker URL/session with its worker token. A branch agent needing more
values sends a names-only request to the coordinator; the coordinator supplies
the **current** parent session ID and runs:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py add \
  --worker WORKER_ID --parent-session "$CURRENT_PARENT_SESSION" --names new_value
```

Additional coordinator operations:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py list
python /absolute/path/to/marimo-pair/scripts/branch.py stop --worker WORKER_ID
```

`add` revalidates the fresh session against the original canonical notebook.
Each batch snapshots current parent values and warns that later batches may
differ. Aliases survive only within one batch. Existing worker names (including
agent-created ones) are rejected before contacting the parent. Restore rollback
is attempted; if an error reports rollback failure, stop the worker. Prior
cells remain usable when rollback succeeds. The coordinator must serialize
same-worker `add` and `stop`: there is no per-worker lock.

The shared skill's `allowed-tools` metadata is not per-agent isolation. Use
handoff and harness grants to withhold coordinator commands, direct
`execute-code.sh`, discovery, parent URL/session, and token from branch agents;
scope run access to the assigned worker. The boundary only prevents accidental
misrouting and keeps credentials out of agent prompts/environments; it is not
a sandbox. Worker code is arbitrary code running as the same OS user and can
read the shared registry/other worker token files, dotenv-derived values loaded
in its environment, and potentially the parent token in transient `curl` argv.
Use a separate OS account or container for actual containment; do not treat
private temp files or harness handoff as protection from malicious or
prompt-injected worker code.

### Runtime limits and cleanup

V1 is same-host/local-loopback, same-OS, and token-protected-parent only;
the parent's Python executable and runtime paths must be accessible. Unsupported
values fail with name/type. Worker files are in private per-user temp storage,
but configured dotenv files are loaded, so their values may be readable in the
worker. There is no session TTL. On completion/cancellation, run `stop`. After
interrupted `start`, use `list` to find leftover IDs and stop them after the
`start` process settles. Do not call `stop` concurrently with its `start`. If `stop`
retains files, keep its registry record and retry. For a lost worker, run
`stop` to clean the dead/stale record; never reroute it to the parent. If
startup needs a process-local SSE drainer that cannot survive the short-lived
CLI, `start` cleans up and fails. `start`, `add`, and `run` require Bash with `jq` and
`curl`; on Windows use validated Git Bash/Cygwin Bash, optionally selected via
`MARIMO_PAIR_BASH`. Provide the branch agent only a read-only parent notebook
path or sanitized excerpt for source inspection, without credentials; never
grant live parent file edits.

## Starting marimo

Discover first. If no server is running and the user wants a notebook, use
[finding-marimo.md](finding-marimo.md).
