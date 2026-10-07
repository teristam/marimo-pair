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

For token-authenticated servers, prefer `MARIMO_TOKEN`.

```bash
MARIMO_TOKEN=... bash scripts/execute-code.sh --url http://localhost:2718 -c "1 + 1"
```

`--token` also works, but may expose the token in process listings. If both are
present, `--token` overrides `MARIMO_TOKEN`. The script sends the token as
`Authorization: ******` on session discovery and code execution requests.

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

This is an optional coordinator workflow; ordinary pairing continues to use
`execute-code.sh` and server discovery as described above. A branch worker is
not selected through discovery. It has a unique worker ID, loopback URL,
session, token, private temporary notebook, and kernel. The coordinator starts
it from explicitly named values in an authenticated parent session:

```bash
MARIMO_TOKEN="$PARENT_TOKEN" python /absolute/path/to/marimo-pair/scripts/branch.py start \
  --parent-url http://127.0.0.1:2718 --parent-session "$PARENT_SESSION" \
  --names records latest_model
```

Before this, the coordinator must run and await the required parent `help(cm)`
command using `execute-code.sh` with explicit `--url` and `--session`. The
worker is only handed off after startup creates and validates its session,
runs its own first code-mode inspection, and restores all requested names.
Use `python3` if needed instead of `python`. The parent remains ordinary
edit-mode and is not globally read-only; keep it idle during export because
pickle hooks may execute user code. Workers do not replay parent preprocessing
or copy the parent graph.

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
MARIMO_TOKEN="$PARENT_TOKEN" python /absolute/path/to/marimo-pair/scripts/branch.py add \
  --worker WORKER_ID --parent-session "$CURRENT_PARENT_SESSION" --names new_value
```

Additional coordinator operations:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py list
python /absolute/path/to/marimo-pair/scripts/branch.py stop --worker WORKER_ID
```

`add` revalidates the supplied session against the same canonical notebook as
the original parent; refresh the session ID after a browser reconnect. Each
batch snapshots the parent's current values and prints a warning that later
batches can differ from earlier ones. Aliases are preserved only within one
batch. Names already defined in the worker (including agent-created names)
are rejected before contacting the parent: request a different name or restart
the worker with all values needed together. If restore fails, the new bootstrap
cell/artifacts are rolled back and prior worker cells remain usable. Serialize
`add` and `stop` operations for the same worker through the coordinator.

The shared skill's `allowed-tools` metadata does **not** enforce per-agent
isolation. The coordinator must use handoff plus harness-level tool grants to
withhold `add`, `start`, `list`, `stop`, direct `execute-code.sh`, discovery,
parent URL/session, and parent token from branch agents, and scope their run
grant to their assigned worker. Never put the parent token in a worker-agent
environment. Parent export uses `execute-code.sh`, whose bearer token appears
in `curl`'s argv; do not promise that the token is absent from every child
process argument. This is not protection from other processes running as the
same OS user.

### Runtime limits and cleanup

V1 is same-host/local-loopback and same-OS only, and needs the parent's Python
executable, import/config paths, and interpreter environment accessible to
the worker. Unsupported or unpickleable values fail with the name/type; this
is a value snapshot, not a clone of process state. Worker notebooks, tokens,
and transfer artifacts live in a private per-user temporary area; dotenv
secret values are not copied. There is no worker session TTL. On completion
or cancellation, the coordinator runs `stop` to shut down the kernel/server
and remove temporary files. If identity/session validation says a worker is
lost, discard it; never reroute its agent to the parent.

## Starting marimo

Discover first. If no server is running and the user wants a notebook, use
[finding-marimo.md](finding-marimo.md).
