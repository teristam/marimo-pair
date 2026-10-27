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

For token-authenticated servers, prefer a token file: set `MARIMO_TOKEN_FILE`
(or pass `--token-file PATH`). `MARIMO_TOKEN` also works. Precedence is
`--token` > `--token-file` > `MARIMO_TOKEN_FILE` > `MARIMO_TOKEN`.

```bash
bash scripts/execute-code.sh --url http://localhost:2718 \
  --token-file /path/to/token.txt -c "1 + 1"
```

The script hands `curl` the `Authorization` header through a private
temporary file (`curl -H @file`, removed on exit), so the token never appears
in `curl`'s process arguments. This needs curl 7.55 or newer. `--token TOKEN`
still works but exposes the token in the script's own arguments and prints a
warning. Token files may end in a newline; a token with any other control
character is refused.

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
is rejected. The coordinator needs the parent URL (`localhost` or `127.0.0.1`)
and a token file from the user. `branch.py` finds the parent session itself:
by `--parent-notebook` path, by an exact `--parent-session` (listing key or
`session_id`), or the lone open session. Ambiguity is an error that lists the
open sessions; nothing is guessed.

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py start \
  --parent-url http://localhost:2718 --parent-notebook /abs/path/notebook.py \
  --token-file /path/to/token.txt --count 3 --names records latest_model
```

`--count N` (1-16) exports the named values once and starts N workers from
that one snapshot, printing a JSON list (without `--count`, one JSON object).
Startup is all-or-nothing: if any worker fails to start, every worker from that
command is stopped. After `start` returns, workers are independent; a failing
`run` affects only its worker.

Before this, the coordinator must run and await the required parent `help(cm)`
command using `execute-code.sh` with explicit `--url` and `--file /abs/path/notebook.py`. The
worker is only handed off after startup creates and validates its session,
runs its own first code-mode inspection, and restores all requested names.
Use `python3` if needed. Supply the parent token with `--token-file` (or `MARIMO_TOKEN_FILE`); do not
use an inline environment assignment, which may not match allowed command
patterns. Never pass the parent token or token file to branch agents. The parent is
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
recorded worker URL/session with its worker token. The coordinator already ran
`help(cm)` for this worker; the branch agent does not use discovery or direct
`execute-code.sh`. A branch agent needing more
values sends a names-only request to the coordinator; the coordinator runs `add`, which finds the parent's **current** session for the
worker's notebook itself (browser reconnects rename sessions):

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py add \
  --worker WORKER_ID --token-file /path/to/token.txt --names new_value
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
in its environment, and potentially the parent token while it is held by the coordinator's child
processes during export.
Use a separate OS account or container for actual containment; do not treat
private temp files or harness handoff as protection from malicious or
prompt-injected worker code.

### Runtime limits and cleanup

V1 is same-host/local-loopback, same-OS, and token-protected-parent only;
the parent's Python executable and runtime paths must be accessible. Workers
share that Python environment and the parent's working directory; branch agents
must not change packages or write project-relative files without approval.
Coordinator and branch-agent commands must share the same `TMPDIR`/`TEMP`/`TMP`
selection or private `MARIMO_PAIR_HOME` root to resolve the same worker ID. Unsupported values fail
with name/type. Worker files are in private per-user temp storage,
but configured dotenv files are loaded, so their values may be readable in the
worker. There is no session TTL. On completion/cancellation, run `stop`. After
interrupted `start`, use `list` to find leftover IDs and stop them after the
`start` process settles. Do not call `stop` concurrently with its `start`. If `stop`
retains files, keep its registry record and retry. For a lost worker, run
`stop` to clean the dead/stale record; it may still exit nonzero after cleanup,
so check `list` to confirm. Never reroute it to the parent. If
startup needs a process-local SSE drainer that cannot survive the short-lived
CLI, `start` cleans up and fails. `start`, `add`, and `run` require Bash with `jq` and
`curl`; on Windows use validated Git Bash/Cygwin Bash, optionally selected via
`MARIMO_PAIR_BASH`. Provide the branch agent only a read-only parent notebook
path or sanitized excerpt for source inspection, without credentials; never
grant live parent file edits.

## Starting marimo

Discover first. If no server is running and the user wants a notebook, use
[finding-marimo.md](finding-marimo.md).
