<h1>
<p align="center">
  /marimo-pair
</h1>
<p align="center">
  reactive Python notebooks as environments for agents
</p>
</p>

<div align="center">
  <video src="https://github.com/user-attachments/assets/d6d3f57a-e997-423c-bf14-8d9fba75e310" width="600" controls></video>
</div>

## Prerequisites

- A running [marimo](https://marimo.io) notebook (`--no-token` for
  ordinary auto-discovery; Branch-and-Explore requires a token-protected parent
  and a token file, preferably via `MARIMO_TOKEN_FILE`/`--token-file` rather
  than `MARIMO_TOKEN`)
- Python, `bash`, `curl`, and `jq` available on `PATH` (on Windows, use Git Bash
  or Cygwin for branch workers; ordinary pairing can also run from WSL, but
  branching across the WSL/Windows OS boundary is unsupported)

## Install

### Agent Skills (any tool)

Works with any agent that supports the [Agent Skills](https://agentskills.io)
open standard:

```bash
npx skills add marimo-team/marimo-pair

# or upgrade an existing install
npx skills upgrade marimo-team/marimo-pair
```

If you don't have `npx` installed but have `uv`:

```bash
uvx deno -A npm:skills add marimo-team/marimo-pair
```

### Claude Code (plugin)

Add the marketplace and install the plugin:

```
/plugin marketplace add marimo-team/marimo-pair
/plugin install marimo-pair@marimo-pair
```

To opt in to auto-updates (recommended), so you always get the latest version:

```
/plugin → Marketplaces → marimo-pair → Enable auto-update
```

## Branch-and-Explore

Use isolated workers to explore mutable copies of selected **live** notebook
values without rerunning the parent's preprocessing. The coordinator needs a
local, token-protected parent, its URL, and a token file (coordinator-only, never a branch agent's).
`branch.py` resolves the parent session itself from `--parent-notebook`; there
is normally no session ID to look up or refresh after a browser reconnect
(`add` reuses the worker's stored one to pick among several sessions on the
notebook). `--parent-session` is needed only when several sessions are open on
the notebook and the stored one is gone. A `--no-token` parent cannot be used
for branching.

Replace `/absolute/path/to/marimo-pair` with the installed skill directory
and use `python3` if `python` is unavailable. Run these commands from the
coordinator, with `PARENT_URL`, `PARENT_NOTEBOOK` (absolute path), and
`TOKEN_FILE` set to the selected parent:

```bash
# Inspect the parent once before using code-mode APIs.
bash /absolute/path/to/marimo-pair/scripts/execute-code.sh \
  --url "$PARENT_URL" --file "$PARENT_NOTEBOOK" --token-file "$TOKEN_FILE" \
  -c "import marimo._code_mode as cm; help(cm)"
# Note: execute-code.sh --file must be the notebook path exactly as the server
# reports it (absolute; no symlink or alternate spelling), unlike branch.py's
# canonical match.

# Start one worker per branch agent; save the returned worker_id as WORKER_ID.
python /absolute/path/to/marimo-pair/scripts/branch.py start \
  --parent-url "$PARENT_URL" --parent-notebook "$PARENT_NOTEBOOK" \
  --token-file "$TOKEN_FILE" --count 3 --names records
```

`--count` prints a JSON list of workers sharing one snapshot; startup is
all-or-nothing.

Give a branch agent only its worker ID and a harness-scoped ability to run
against that ID. Worker code goes on stdin and never selects a server from
global discovery:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py run --worker "$WORKER_ID" - <<'PY'
print(records[:3])
PY
```

If the agent needs another parent value, it sends the coordinator **names only**.
The coordinator runs `add`, which finds the parent's current session itself;
later batches snapshot the parent's *current* state, and existing worker names
cannot be replaced:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py add \
  --worker "$WORKER_ID" --token-file "$TOKEN_FILE" --names latest_model
python /absolute/path/to/marimo-pair/scripts/branch.py list
python /absolute/path/to/marimo-pair/scripts/branch.py stop --worker "$WORKER_ID"
```

Never hand a branch agent the parent token, token file, URL, `add`, direct
`execute-code.sh`, or discovery access. The skill's shared `allowed-tools` is
**not** per-agent isolation; enforce worker-specific grants in your harness.
Workers share the parent's OS user, Python environment, working directory, and
configured dotenv values: this prevents accidental kernel misrouting, **not**
a malicious worker from accessing same-user files or secrets. Coordinate
same-worker `add`/`stop` operations sequentially, and keep the coordinator and
agent on the same OS and temporary registry root (set the same private
`MARIMO_PAIR_HOME` if their temp directories differ). See the
[skill workflow](skills/marimo-pair/SKILL.md#branch-and-explore-workers) for
runtime limits and cleanup details.

## FAQ

### I keep getting prompted to allow Bash commands

The skill declares its own `allowed-tools`, but Claude Code may still prompt
you to approve each Bash call. To avoid repeated prompts, copy the absolute
paths to the scripts from the installed skill and add them to your
`.claude/settings.json` (project-level) or `~/.claude/settings.json` (global):

```json
{
  "permissions": {
    "allow": [
      "Bash(bash /path/to/skills/marimo-pair/scripts/discover-servers.sh *)",
      "Bash(bash /path/to/skills/marimo-pair/scripts/execute-code.sh *)"
    ]
  }
}
```

Those broad permissions are for ordinary coordinator pairing, **not** branch
agents. Grant branch agents only their specific worker's `branch.py run`
capability; don't globally allow `branch.py *` on their behalf.

## Contributing

We'd love your help in improving this skill. After a marimo pair session,
please run the `/retro-marimo-pair` command to share feedback on how we can
improve the skill. You can also file an issue on the
[GitHub repository](https://github.com/marimo-team/marimo-pair/issues).
