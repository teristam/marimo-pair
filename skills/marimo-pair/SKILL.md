---
name: marimo-pair
description: >-
  Drive a live marimo notebook as a workspace: run Python in the same kernel
  the user does, inspect live notebook state, and commit durable notebook
  changes. Use when the user wants to start a marimo notebook or pair on an
  active marimo session.
allowed-tools: Bash(bash **/scripts/discover-servers.sh *), Bash(bash **/scripts/execute-code.sh *), Bash(python **/scripts/branch.py *), Bash(python3 **/scripts/branch.py *), Read
---

marimo is a reactive Python runtime for building reproducible Python programs
(marimo notebooks). Cells are connected by the variables they define and
reference. Running a cell re-executes dependents in dataflow order. The active
runtime holds the kernel namespace, cell state, and dataflow graph. The
notebook (`.py` file) is the artifact the kernel writes from that state while a
session is running.

A user interacts with the same runtime via a notebook UI with cells, outputs,
and widgets.

**WARNING. The active runtime is the source of truth.** During a session, you
SHOULD NOT modify the associated `.py` file directly. File edits WILL NOT reach
the active kernel or user, and the kernel may overwrite them on save. Use
`marimo._code_mode` (`cm`) for notebook changes. Reading disk is fine, but
prefer `ctx.cells[...].code` for current cell code.

The harness reports the absolute path to this `SKILL.md`. Resolve bundled
`scripts/...` and `reference/...` paths from its parent directory, even when
the current working directory is a notebook workspace. In command examples,
replace `/absolute/path/to/marimo-pair` with that directory.

## Required first kernel command

For ordinary pairing or coordinator parent access, start every code-mode session
with this dedicated command. A handed-off branch worker has already had this
inspection run by the coordinator: its agent uses only `branch.py run --worker`
and skips direct `execute-code.sh` and discovery.

Start with:

```bash
bash /absolute/path/to/marimo-pair/scripts/execute-code.sh \
  --url http://localhost:2718 \
  -c "import marimo._code_mode as cm; help(cm)"
```

For each coordinator-connected kernel, including read-only tasks, follow:

1. Run the inspection command once.
2. Wait for successful `help(cm)` output.
3. Then use `cm.get_context()` or another `cm` API in a later call.

Do not run task-specific `cm` code before the inspection command succeeds.

## Branch-and-Explore Workers

Use this workflow when parallel exploration needs mutable copies of live
values without rerunning expensive parent preprocessing. Ordinary single-kernel
pairing below remains unchanged.

Branching requires a token-protected parent; a `--no-token` server is rejected.
The coordinator needs the parent URL (`localhost` or `127.0.0.1`) and a token
file from the user. `branch.py` resolves the parent session itself from
`--parent-notebook`, an exact `--parent-session`, or the lone open session, and
errors (listing sessions) instead of guessing. Keep parent credentials
coordinator-only.

Pass the parent token as `--token-file PATH` (or set `MARIMO_TOKEN_FILE`) on
`start` and `add`; avoid `--token` and inline environment assignments. Tokens
reach `curl` through a private header file, not process arguments.

First run and await the required `help(cm)` command above against the parent
using its explicit URL and notebook path:

```bash
bash /absolute/path/to/marimo-pair/scripts/execute-code.sh \
  --url http://localhost:2718 --file /abs/path/notebook.py \
  --token-file /path/to/token.txt \
  -c "import marimo._code_mode as cm; help(cm)"
```

`--file` must be the notebook path exactly as the server reports it (absolute;
no symlink or alternate spelling), unlike `branch.py`'s canonical match.

After it succeeds, start one private worker per branch agent with a single
export of only the requested names:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py start \
  --parent-url http://localhost:2718 --parent-notebook /abs/path/notebook.py \
  --token-file /path/to/token.txt --count 3 --names records latest_model
```

`--count N` (1-16) prints a JSON list of workers sharing one snapshot; startup
is all-or-nothing. Once started, workers fail independently.

Each started worker reports:
- `run_command`: a launcher that can only run code in that one worker
- `environment`: the Python version, plus the installed version (or `null`) of
  common analysis packages, which tells you what branch agents can import
  without installing anything. Add more modules with `--probe MODULE ...`.

Use `python3` instead of `python` where that is the available interpreter. The
coordinator retains the returned `worker_id` and hands each agent only its
`run_command`. Worker code is sent on stdin, never routed by discovery or an
arbitrary URL:

```bash
bash /tmp/marimo-pair-.../temp/WORKER_ID/run - <<'PY'
print(records[:3])
PY
```

A plain run is a scratchpad: new top-level names are discarded afterwards. To
keep results between calls, pass `--cell NAME`. It creates the worker cell
`NAME`, or replaces it on later calls, runs it, and keeps its definitions for
later runs. marimo's graph rules apply: each name is defined by one cell, so
redefine a name by re-running the same `--cell NAME`. A failing cell makes the
command exit nonzero. `branch.py run --worker WORKER_ID [--cell NAME] -` is the
same operation without the launcher.

Branch agents follow [branch-agents.md](reference/branch-agents.md), which has
a prompt template and the rules for combining branching with other skills.

The coordinator alone may inspect `branch.py list`, request additional values,
and stop workers:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py list
python /absolute/path/to/marimo-pair/scripts/branch.py add \
  --worker WORKER_ID --token-file /path/to/token.txt --names new_value
python /absolute/path/to/marimo-pair/scripts/branch.py stop --worker WORKER_ID
```

For each later batch, the branch agent sends the coordinator a **names-only**
request; it must not invoke `add`. The coordinator runs `add`, which resolves
the parent's current session for the worker's notebook and revalidates it.
Every batch is a snapshot of the parent's current state; keep the parent idle
while exporting, because serialization hooks may run user code. Batch aliases
are retained only within a single batch. Reject collisions with names already
in the worker; choose new names or restart the worker to refresh existing
values. Restore rollback is attempted. If an error reports rollback failure, stop
that worker rather than continuing with uncertain state; earlier cells remain
usable when rollback succeeds.

Branch agents receive only their worker's `run_command`, and may ask the
coordinator for more names. Grant it per agent in the harness, e.g.
`Bash(bash /tmp/marimo-pair-.../temp/WORKER_ID/run *)`. The launcher accepts only
`-` or `--cell NAME -`, runs nothing but `run` for that worker, and drops
`MARIMO_TOKEN`/`MARIMO_TOKEN_FILE`. Never give agents `branch.py` itself,
`add`, `start`, `list`, `stop`, the parent URL/session, the parent token or its
token file, `execute-code.sh`, or server-discovery access. The shared
`allowed-tools` list is not per-agent isolation: harness grants and handoff
must withhold those broader capabilities.
This only prevents accidental misrouting and keeps credentials out of agent
prompts/environments; it is **not a sandbox**. Worker code is arbitrary code
running as the same OS user and can read the shared registry/other worker token
files, dotenv-derived values loaded into its environment, and potentially the
parent token while the coordinator's child processes hold it during export.
Use a separate OS account or container for actual containment. Do not pass the
parent token, its token file, or `MARIMO_TOKEN`/`MARIMO_TOKEN_FILE` into a
branch agent's environment.

Workers use separate temporary notebooks and kernels; they do not copy the
parent graph or rerun preprocessing. They still share the parent's Python
environment and working directory: branch agents must not change packages or
write project-relative files without user/coordinator approval. Files stay in
a private per-user temp area, but dotenv files are loaded by workers, so their
values may be visible to worker code; not copying those files is not a secrecy
boundary. V1 requires a
token-protected, loopback parent and worker on the same host/OS with
reproducible paths and the parent's Python environment. Unsupported values
(such as notebook-local classes/functions, UI/state/runtime objects, or
unpickleable resources) fail with an attributable error. Workers have no
session TTL; stop them explicitly. If `start` is interrupted, use `list` to
find leftover IDs, then `stop` after the `start` process settles. Do not run
`stop` concurrently with a worker's in-progress `start`. If `stop` retains
files, keep its record and retry. For a lost worker, run `stop` to clean its
dead/stale record; it may still exit nonzero after cleanup, so check `list` to
confirm. Never reroute it to the parent.

For source inspection, give the branch agent a read-only parent notebook path
or sanitized excerpt, without credentials; never grant edits to the live
parent artifact. `start`, `add`, and `run` need Bash plus `jq` and `curl`; on Windows
use validated Git Bash/Cygwin Bash (`MARIMO_PAIR_BASH` can override). If
startup requires a process-local SSE drainer that cannot outlive the short
CLI, `start` cleans up and fails. Serialize same-worker `add` and `stop`
operations yourself: the registry has no per-worker lock. Coordinator and
branch-agent commands must share the same `TMPDIR`/`TEMP`/`TMP` selection or
private `MARIMO_PAIR_HOME` root so the worker ID resolves to the same registry.

The parent remains an ordinary edit-mode server, not globally read-only; this
workflow only withholds arbitrary parent execution from branch agents.

## Connect to a Notebook

Use the bundled `execute-code.sh` from the reported skill directory or MCP
(`execute_code(...)`) to run Python in a live marimo kernel.

`execute-code.sh` always takes `--url`. If the user provides a notebook URL,
run the required inspection against it directly:

```bash
bash /absolute/path/to/marimo-pair/scripts/execute-code.sh \
  --url http://localhost:2718 \
  -c "import marimo._code_mode as cm; help(cm)"
```

After that command succeeds, pass task code with `-c CODE`, `-` for stdin, or
a file path:

```bash
bash /absolute/path/to/marimo-pair/scripts/execute-code.sh \
  --url http://localhost:2718 - <<'PY'
import marimo._code_mode as cm

async with cm.get_context() as ctx:
    cid = ctx.create_cell("x = df.head()")
    ctx.run_cell(cid)
PY
```

If the user gives no URL, find or start a notebook. Look for a running server
with `bash /absolute/path/to/marimo-pair/scripts/discover-servers.sh`, MCP
`list_sessions()`, or local process context, and pass the `url` it reports to
`--url`. With one notebook open, the script targets it automatically; with
several, pass `--file` with the notebook's file key.

If no server is running and the user wants ordinary pairing, start marimo with
`--no-token` (and without `--headless`) so it auto-registers for discovery. The
notebook UI must be open for `execute-code` to target it. Such a `--no-token`
server cannot be a branch parent; branching requires token authentication.
The right invocation depends on context (project tooling, global install,
sandbox mode). If the
notebook file contains a PEP 723 `#
/// script` header, it MUST be opened with `--sandbox` — otherwise marimo
ignores the inline dependencies. See
[finding-marimo.md](reference/finding-marimo.md) for the full decision tree and
[execution-context.md](reference/execution-context.md) for selector resolution,
scripts, MCP, and shell quoting.

## Scratchpad Scope

`execute-code` evaluates Python in marimo's scratchpad: a temporary namespace
with a shallow copy of the kernel globals. Notebook variables are available by
name, but new top-level bindings and rebindings are discarded after each call.
In-place mutations to notebook-owned objects can persist because those names
still reference live objects.

Each call reports stdout and stderr from the scratchpad, plus console output
from notebook cells it causes to run, including reactive descendants.

### Ordinary Python

Use ordinary Python in the scratchpad to inspect variables, sample data, test
transformations, probe APIs, check imports, and read widget state.

```python
print(df.head())

x = 10
print(x)
```

Here `df` comes from notebook globals, while `x` is a scratchpad-local binding.
`x` exists for this call only and WILL NOT be added to notebook globals.

### Persist with `cm`

Top-level scratchpad assignments and rebindings are temporary. To persist work,
including new variables, you MUST submit changes through `marimo._code_mode`
(`cm`).

`marimo._code_mode` is a PRIVATE, UNSTABLE agent API (note the leading
underscore). It exists for tools like this skill to drive a live kernel from
the scratchpad. DO NOT import it from notebook cells, library code, or
anything a user would run — methods can change or disappear across marimo
versions and kernels. Treat every `import marimo._code_mode as cm` as
scratchpad-only.

Open a code-mode context to queue notebook changes.

```python
import marimo._code_mode as cm

async with cm.get_context() as ctx:
    cid = ctx.create_cell("x = df.head()")
    ctx.run_cell(cid)
```

The scratchpad supports top-level async code. Use `async with` directly;
wrapping it in `asyncio.run(...)` is unnecessary and can conflict with the
kernel's event loop.

After this block exits and the new cell runs, `x` is notebook state. Later
scratchpad calls can read `x` by name. Code later in the same scratchpad call
should read `ctx.globals["x"]`, because the scratchpad namespace was copied
before the cell ran.

Inside the context, queued mutation methods are synchronous. Call them
directly; do not `await` them. Each call queues an operation for marimo to
apply when the context exits normally. If the block raises, the queue is
discarded.

On clean exit, marimo applies packages, validates and applies structural cell
changes, runs queued cells, then may run dependents. Validation is only
structural since queued cell runs can still error. `create_cell` and
`edit_cell` change notebook structure only. Use `run_cell` to execute. 

`create_cell` currently defaults to `hide_code=True`, which collapses the code
editor in the UI. Pass `hide_code=False` if the user wants created cells to
be visible without manually expanding them.


## Marimo Rules

marimo imposes a small contract on notebook code so it can keep the notebook as
a directed acyclic graph (DAG):

- **No cycles** - cells cannot depend on each other in a cycle.
- **No public redefinitions across cells** - each name has one owning cell.
- **No wildcard imports** - `import *` prevents static analysis of definitions.

These rules keep the kernel, UI, and saved artifact consistent.

When `cm` submits a cell body, marimo parses its top-level definitions and
references. Public names enter the graph. Names that start with `_` are local
to their cell and unavailable to other cells. If a `cm` edit violates the
contract, marimo rejects the structural change and returns the validation
error.

## The Notebook's Shape

A notebook is an ordered collection of cells. `ctx.cells` is the document view
and `ctx.graph` is the dataflow view.

```python
for cell in ctx.cells:
    cell  # .id, .code, .name, .config, .status, .errors

ctx.cells["setup"]         # by name
ctx.cells[0]               # by position
list(ctx.cells.keys())     # all IDs, in notebook order
```

Cell IDs are opaque strings which can be queried from the notebook or captured
from `cm` return values:

```python
cid = ctx.create_cell("df = pd.read_csv('data.csv')")
print(cid)   # e.g. 'Hbol'
```

Alternatively, cells can be assigned and referenced by `name`. The graph can be
used to understand its role in the dataflow.

```python
for cid, impl in ctx.graph.cells.items():
    impl  # .defs, .refs   (sets of public names)

ctx.graph.descendants(cid)   # cells that re-run when this one changes
ctx.graph.ancestors(cid)     # cells this one depends on
```

In marimo, deletes are *destructive* so it can be useful to query the
descendants prior to deleting to understand it's impact.

## Writing Notebook Changes

The graph contract keeps marimo able to run and save the notebook. Passing
those checks alone does not guarantee a useful artifact. Committed cells should
still be readable, rerunnable, and editable.

Make durable edits that reuse the notebook's existing names, imports,
dependencies, and UI model. Don't be lazy. Avoid one-off workarounds that pass
`cm` validation but leave a brittle notebook.

### Cell Bodies

Submit the code that belongs in the cell.

- **Submit cell contents** - `create_cell` and `edit_cell` take cell contents,
  not saved-file `@app.cell` wrappers.
- **Read before replacing** - for now, another editor may change a cell between
  scratchpad calls. Before `edit_cell`, read the current body from
  `ctx.cells[...]` and submit the full replacement.
- **Reuse notebook imports** - if `np` already exists, use it or edit the owning
  import cell. DO NOT add `import numpy as _np` just to bypass the graph.
- **Define each public name once** - a public name has one owning cell.
  Reassigning it in another cell fails with `Multiply-defined names`; edit the
  owning cell or give the result a new name. See
  [gotchas.md](reference/gotchas.md).
- **Run cells deliberately** - `create_cell` and `edit_cell` change structure
  only. Queue `ctx.run_cell(...)` when the cell should execute.

### Cell Boundaries

A cell is also a rerun boundary. Put expensive or reusable computation
upstream of presentation so UI edits stay cheap. Keep cheap,
presentation-specific work with the view when that is easier to read.

Use `mo.vstack` and `mo.hstack` only when the composition is part of the UI.
Narrative often reads better in an adjacent markdown cell.

### Prefer `cm`-Managed Changes

Use `cm` APIs when they exist. Avoid direct file edits, shell package commands,
and scratchpad-only state for changes that should persist.

- **Do not edit the `.py` artifact** - DO NOT use `Edit`, `Write`, or
  `NotebookEdit` on the notebook file during a live session. Use
  `ctx.edit_cell(...)` even for small changes.
- **Manage packages through `cm`** - use `ctx.packages.add()` or
  `ctx.packages.remove()` instead of direct `uv` or `pip`; confirm
  non-obvious dependency changes.
- **Avoid transient paths** - persisted cells should not depend on `/tmp/...`
  unless the work is intentionally transient.
- **Delete deliberately** - deleting a cell removes globals it defines. Reuse
  empty cells when convenient and delete cells left empty after edits.

### UI and Widgets

Inspect the object before changing it. Different UI objects update through
different paths.

- **Set `mo.ui.*` through `cm`** - use `ctx.set_ui_value(element, value)` inside
  `cm.get_context()`.
- **Set anywidget traitlets directly** - synced traitlets are Python
  attributes, for example `widget.value = 5`.

For designing custom visual or interactive output, see
[rich-representations.md](reference/rich-representations.md).

## References

- [execution-context.md](reference/execution-context.md) — scripts, MCP, auth, startup, and shell quoting
- [branch-agents.md](reference/branch-agents.md) — branch-agent prompt template and combining branching with other skills
- [finding-marimo.md](reference/finding-marimo.md) — choosing the right marimo invocation
- [gotchas.md](reference/gotchas.md) — name redefinition, cached module proxies, and notebook traps
- [rich-representations.md](reference/rich-representations.md) — custom widgets and visualizations
- [notebook-improvements.md](reference/notebook-improvements.md) — improving existing notebooks
