# Branch-and-Explore Worker Kernels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement this plan task-by-task, with review between tasks.

**Goal:** Add a marimo-pair-only workflow that exports selected live notebook values into a separate mutable worker kernel for each agent, avoiding rerun of expensive preprocessing.

**Architecture:** A restricted parent exporter serializes requested batches and the effective parent import/runtime context. A standard-library controller starts one authenticated headless marimo server per worker, explicitly opens and validates its SSE session, runs a worker-local bootstrap cell, and routes execution by worker ID. Branch agents can send names-only requests for later batches, but the coordinator alone runs `branch.py add` and the parent exporter; workers never receive the parent URL, token, or a generic parent executor. Workers use unique temporary notebook paths and never copy the parent's graph or source.

**Tech Stack:** Python 3.10+ (the bundled marimo package requires `>=3.10`), Python standard library (`pickle`, `subprocess`, `urllib`, `socket`, `json`, `concurrent.futures`, `unittest`, `ctypes`), existing Bash `execute-code.sh`, marimo's existing `_code_mode` API, and the experimental SSE endpoint.

**Spec:** `docs/superpowers/specs/2026-10-06-marimo-pair-branch-and-explore-design.md`

## Global Constraints

- All feature code, tests, and docs stay in marimo-pair; do not modify upstream marimo.
- V1 is same-host/loopback only. Reject remote parents and Windows/WSL cross-boundary cases if the controller cannot access the parent's executable, notebook, required runtime paths, and artifacts as local paths.
- Transfer only explicitly requested valid Python variable names; never execute agent-supplied code in the parent.
- Every worker execution uses an explicit worker URL and session ID plus that worker's token. Never use global discovery to select a worker.
- Start workers with per-worker auth via `--token-password-file`, not `--no-token`; launch with global `-q` before `edit`, never persist parent tokens, and redact token/query values before displaying any log text.
- Worker servers use `--no-skew-protection` because the existing shutdown POST otherwise requires a random `Marimo-Server-Token`; retain normal bearer-token auth.
- Start worker servers without `--session-ttl`; shut them down explicitly.
- Reuse the parent's Python executable and `cwd`, but keep worker directories and artifacts in a per-user private temp area, not the user's project tree. Capture the effective absolute `runtime.pythonpath` and `runtime.dotenv` paths from the merged project and notebook script configuration, then place those paths in a valid PEP 723 `[tool.marimo.runtime]` header in each minimal worker notebook. Never copy dotenv values. Fail before handoff if a required path cannot be reproduced or accessed.
- Launch workers with `--no-sandbox` and the parent's interpreter, so no new sandbox or dependency prompt is created; pass `--skip-update-check`.
- The first worker code-mode command must succeed with `import marimo._code_mode as cm; help(cm)` before bootstrap uses `cm` APIs.
- Parent export is a coordinator-hosted restricted capability receiving only worker ID, fresh parent session ID, and variable names. A branch agent sends a names-only request to the coordinator; the coordinator runs `branch.py add` with the parent token. The token never enters the worker kernel or branch-agent environment. Keep the parent idle during export because user-defined pickle hooks may execute. The parent remains a normal edit-mode server and is not globally read-only.
- Unsupported values fail with their name/type; never silently drop or substitute a value. Batch artifacts are private, local, trusted-only pickle data, and are deleted after bootstrap/cleanup.
- Use Python `-B` for tests and temporarily set/restore `sys.dont_write_bytecode` before dynamically importing helpers; set `PYTHONDONTWRITEBYTECODE=1` for child servers. Avoid repository `__pycache__` artifacts.

---

## File Structure

- Create `skills/marimo-pair/scripts/branch_state.py`: name validation, batch serialization, parent runtime capture, parent-export source generation, worker notebook/header generation, and explicit worker bootstrap source generation.
- Create `skills/marimo-pair/scripts/branch.py`: worker process/session lifecycle, per-worker registry, explicit routing, CLI, and the restricted export/run commands.
- Create `tests/__init__.py`, `tests/fixtures/__init__.py`, and `tests/fixtures/branch_models.py` for standard-library test discovery and a module-level data class.
- Create `tests/fixtures/branch_parent.py`: a minimal parent notebook with a marker-writing preprocessing cell and transferable values.
- Create `tests/test_branch_state.py` and `tests/test_branch.py` for standard-library unit tests.
- Create `tests/test_branch_integration.py` for opt-in local-marimo integration coverage.
- Modify `skills/marimo-pair/SKILL.md` and `skills/marimo-pair/reference/execution-context.md` for branch roles, tools, routing, and lifecycle.
- Keep `discover-servers.sh` unchanged. Update `skills/marimo-pair/scripts/execute-code.sh` to stream code from all existing input modes through `jq -Rs` into `curl --data-binary @-` rather than placing source/JSON bodies in child-process arguments; preserve its CLI and session/auth/error behavior. The controller must locate a usable Bash explicitly and call it with code on stdin; it must not rely on server discovery for workers.
- Plugin manifests `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`, and `.agents/plugins/marketplace.json` only point to the skill root; do not modify them.

## Task 1: Implement named-value serialization and runtime capture

**Files:**
- Create: `skills/marimo-pair/scripts/branch_state.py`
- Create: `tests/__init__.py`
- Create: `tests/test_branch_state.py`
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/branch_models.py`
- Create: `tests/fixtures/branch_parent.py`

**Interfaces:**
- `ParentRuntime`: parent notebook path, discovered project/config sources, `cwd`, `sys.path`, Python executable/version, effective absolute `runtime.pythonpath` entries, effective absolute dotenv paths, and sandbox/dependency context. Capture the merged marimo config; use its already-resolved project/script/default dotenv paths, resolving only remaining relative user-config paths with marimo's resolver (nearest `pyproject.toml` root, otherwise notebook directory). Do not serialize dotenv secret values.
- `BranchStateError(ValueError)` identifies the variable and type when export/restore fails.
- `validate_names(names: Sequence[str], existing: Collection[str] = ()) -> list[str]`; reject Python keywords, invalid identifiers, all names beginning with `_` (marimo cell-private names), and collisions.
- `write_batch(values: Mapping[str, object], path: Path) -> None`
- `read_batch(path: Path) -> dict[str, object]`
- `export_named_values(scope, names, bundle_path, metadata_path, notebook_path) -> None`
- `read_parent_runtime(metadata_path: Path) -> ParentRuntime` validates captured local paths/executable accessibility without requiring the controller to run the parent interpreter.
- `verify_runtime_environment(runtime: ParentRuntime) -> None` checks interpreter/version/prefix identity inside the worker bootstrap, not on the controller.
- `build_parent_export_code(names, bundle_path, metadata_path, helper_path, notebook_path) -> str`
- `build_worker_notebook(runtime: ParentRuntime) -> str` returns a valid minimal notebook with a PEP 723 script header containing only the effective absolute runtime paths.
- `build_worker_bootstrap_cell(names, bundle_path, metadata_path, helper_path) -> str`

- [ ] **Step 1: Write failing tests and test fixtures**

Write unit tests for keyword/private-name rejection (including every leading-underscore name), alias identity across repeated records in one batch, module-level class roundtrip, nested `__main__` rejection through `__reduce__`/pickle traversal, `Unpickler.find_class` protection, a `__getstate__` object that omits a lock, and partial-artifact deletion. Add fake nested marimo UI and runtime/state values and assert attributable name/type errors. Test that runtime metadata excludes dotenv secret values, captures resolved absolute `runtime.pythonpath`/dotenv paths from project and script config, tolerates a missing default `.env`, rejects an existing unreadable required path, and lets a controller with a different interpreter read metadata while worker-only verification rejects a mismatched interpreter. Assert generated parent export and worker bootstrap programs execute against temporary scopes/artifacts (not only compile), and that generated helper modules are registered before `exec_module`. Assert the generated worker notebook has a valid PEP 723 header plus `import marimo`/`app = marimo.App()`, and generated bootstrap source has explicit assignments, unique private helper names, and no `globals().update`. Round-trip Windows paths containing backslashes, spaces, and quotes through the generated TOML header and marimo's `script_metadata.loads`. Add config fixtures for a project `pyproject.toml`, a no-project parent with and without notebook-adjacent `.env`, and script-header `[tool.marimo.runtime]` settings, including dotenv resolution against the project root when a `pyproject.toml` exists.

Create `tests/fixtures/branch_models.py` with a small module-level dataclass. Create `tests/fixtures/branch_parent.py` with one cell that reads `MARIMO_PAIR_MARKER_PATH`, writes a marker once, defines `records = [1]`, and defines an instance of the fixture dataclass.

- [ ] **Step 2: Run the state tests and confirm they fail**

Windows PowerShell/Git Bash: `python -B -m unittest discover -s tests -p "test_branch_state.py" -v`

macOS/Linux: `python3 -B -m unittest discover -s tests -p "test_branch_state.py" -v`

Expected: FAIL because `branch_state.py` and its public helpers do not exist yet.

- [ ] **Step 3: Implement serialization and safe code generation**

Use one validating `pickle.Pickler` and one guarded `pickle.Unpickler` for all per-name records in a batch so memo identity is shared. Validate objects as pickle traverses their reducer output (for example with `persistent_id`) rather than walking raw `__dict__`, so `__reduce__` references cannot hide notebook `__main__` values and `__getstate__`-excluded locks are not falsely rejected. Guard `find_class` against `__main__`, marimo UI, and state/runtime classes during restore. Attribute each failure to the current name/type and delete the whole artifact.

Capture the effective merged config using `get_default_config_manager(current_path=parent_notebook).get_config(hide_secrets=False)`; retain only runtime paths/settings, never dotenv values. Use merged `runtime.dotenv` paths directly when marimo has resolved them; resolve only remaining relative user-config entries with marimo's dotenv resolver. Preserve missing dotenv paths because marimo ignores absent files; reject existing non-regular or unreadable dotenv paths. Record effective absolute Python import roots (checking them against live parent `sys.path`) and absolute dotenv paths. Include any parent-only import root needed at runtime in the generated header, or reject startup if it cannot be reproduced safely. Generate each private-temp worker notebook with a valid PEP 723 header containing those paths under `[tool.marimo.runtime]`, followed by `import marimo` and `app = marimo.App()`; use TOML-safe strings. Do not depend on the worker temp directory being under the user's project. `read_parent_runtime` validates local path/executable accessibility without comparing against the coordinator's Python; `verify_runtime_environment` runs inside the worker bootstrap to compare interpreter/version/prefix identity and then restore parent `cwd` and `sys.path` before unpickling. If the parent executable, any required import/dotenv path, sandbox environment, or artifact path cannot be reproduced, fail before handoff.

Generate parent and worker code only from validated identifier tokens and literal-safe paths. Temporarily set and restore `sys.dont_write_bytecode` before importing the helper. Use unique `_marimo_pair_<batch-id>_...` helper identifiers for every batch so helper definitions cannot collide with user or later-batch names.

- [ ] **Step 4: Run state tests and confirm they pass**

Run the same Windows/POSIX commands from Step 2.
Expected: PASS for names/private names, reducer-hidden unsupported values, guarded restore, local-module values, batch aliases, missing dotenv defaults, project/script runtime capture, controller/worker interpreter separation, artifact cleanup, and executed generated programs.

- [ ] **Step 5: Commit the independently tested state module**

```bash
git add skills/marimo-pair/scripts/branch_state.py tests/__init__.py tests/test_branch_state.py tests/fixtures/__init__.py tests/fixtures/branch_models.py tests/fixtures/branch_parent.py
git commit -m "feat: add branch state transfer helpers"
```

## Task 2: Implement worker startup, session validation, and cleanup

**Files:**
- Create: `skills/marimo-pair/scripts/branch.py`
- Create: `tests/test_branch.py`
- Create: `tests/test_branch_integration.py`

**Interfaces:**
- `ParentTarget(url: str, session_id: str, notebook_path: Path)`; parent token comes from `MARIMO_TOKEN` and is never persisted.
- `WorkerRecord`: worker ID, worker URL/session, private token path, notebook/temp/runtime paths, parent URL/notebook path/current session ID, status (`starting`/`running`/`stopping`), server PID and process-creation identity, optional drainer PID/identity, imported names, and log path. Persist no live `Popen` handles or token values.
- `WorkerManager.start_worker(parent: ParentTarget, names: Sequence[str]) -> WorkerRecord`
- `WorkerManager.execute(worker_id: str, code: str) -> ExecutionResult`
- `WorkerManager.stop_worker(worker_id: str) -> None`
- `WorkerManager.list_workers() -> list[WorkerRecord]`
- Store one atomically replaced registry file per worker under a per-user directory, avoiding lost updates between parallel `start` commands. Store the token separately with private permissions.

- [ ] **Step 1: Write failing lifecycle tests and the initial end-to-end test**

Unit-test SSE JSON parsing (including whitespace), readiness, `close`, `kernel-startup-error`, premature EOF, bounded timeouts, identity checks, parallel per-worker registry writes, Windows Bash candidate ordering/WSL rejection/Git fallback/override/`$OSTYPE` validation, `jq`/`curl` preflight, and cleanup on startup error/cancellation. Mock shutdown responses and assert every non-2xx is an error. Assert the launch command puts global `-q` before `edit`, has `--no-skew-protection`, `--no-sandbox`, `--skip-update-check`, a token-password file, and no session-TTL flag. Assert `Popen` uses `stdin=DEVNULL`, stdout/stderr directed to a temp log, `close_fds=True`, and a child environment with no parent `MARIMO_TOKEN`; assert startup log text contains no worker token and displayed tails redact `access_token=` values. Assert the execute-script subprocess passes `--url`/`--session` and `-`, supplies code through stdin, and never uses `-c`. Test safe waits without `os.kill(pid, 0)` on Windows and process-identity checks before forced termination. Test that a fresh `WorkerManager` instance can stop a worker using the registry after the start process has exited.

The opt-in integration suite starts a temporary parent server with a token-password file, global `-q` before `edit`, `--no-skew-protection`, `--no-sandbox`, `--skip-update-check`, `PYTHONDONTWRITEBYTECODE=1`, and no session-TTL option. Open a session by authenticated SSE without a `file` query parameter and drain through `kernel-ready`. Before any `/api/kernel/execute` call, POST `/api/kernel/instantiate` with `{"objectIds": [], "values": [], "autoRun": true}`, bearer auth, and the session header. Start with the marker absent. Poll authenticated `/api/kernel/status` with the same session header until the response's `state` field is `idle` **and** the marker exists exactly once; an initial/bare idle state without the marker is not sufficient. Then close SSE. Run the required first code-mode command, `help(cm)`, and wait for it to complete before export. Assert the parent marker was written once before export and the worker startup log contains no token.

Run three runtime-context scenarios: (1) a temporary project with `pyproject.toml` containing `[tool.marimo.runtime] pythonpath = ["src"]` and `dotenv = [".env"]`, a module under `src/`, and a harmless `.env` value; (2) a parent notebook copied into a temp directory with no `pyproject.toml` and a harmless `.env` next to the notebook; and (3) a parent notebook with script-header `[tool.marimo.runtime]` `pythonpath`/`dotenv` settings that override conflicting project settings, verifying script-level precedence and that script dotenv is resolved against the nearest `pyproject.toml` root. For copied-fixture imports, launch the parent server from the repository root or set its `PYTHONPATH` to the repo root. Keep every worker notebook and artifact in its private per-user temp directory. In each case, assert the worker's effective absolute paths match the captured parent paths and that the module/dotenv value is available.

In the no-project scenario, start two workers with `records` and the module-level model; mutate both workers concurrently with `concurrent.futures.ThreadPoolExecutor`. Verify the parent list, marker count, and hash of the actual parent notebook file (including any temp copy) are unchanged; verify the two worker values differ and worker notebook paths differ from the parent. Stop both workers and assert each exits gracefully within 10 seconds without a forced kill; shut down the temporary parent server in test cleanup as well. Unit-test that an inaccessible required runtime path is rejected before `Popen`.

- [ ] **Step 2: Run lifecycle and integration tests and confirm they fail**

Windows PowerShell/Git Bash: `python -B -m unittest discover -s tests -p "test_branch.py" -v`

macOS/Linux: `python3 -B -m unittest discover -s tests -p "test_branch.py" -v`

For integration, set `MARIMO_PAIR_RUN_INTEGRATION=1` and run `python -B -m unittest tests.test_branch_integration -v` on Windows PowerShell/Git Bash or `python3 -B -m unittest tests.test_branch_integration -v` on macOS/Linux. Without the opt-in variable, integration tests skip with a clear reason.

Expected: FAIL because `WorkerManager` and lifecycle code do not exist.

- [ ] **Step 3: Implement worker lifecycle and local execution transport**

Before launch, validate the parent session via authenticated `/api/sessions` and compare normalized real notebook paths. Reject non-loopback parents, inaccessible paths/interpreters/runtime paths, and cross-OS/WSL path boundaries. Create the worker directory and artifact paths in a per-user private temp area, choose the worker ID/session/port, and write a provisional `starting` registry record. Generate the fixed names-only exporter and invoke `execute-code.sh` with explicit parent URL/session and parent token to write the private bundle/runtime metadata. Read the captured effective runtime from that metadata, then generate a minimal worker notebook with its paths in a valid PEP 723 `[tool.marimo.runtime]` header. Abort and clean the record/files before spawning a worker if export or notebook generation fails.

Select Bash explicitly. Honor `MARIMO_PAIR_BASH` first, but validate the executable. On Windows, enumerate every `bash.exe` candidate on `PATH` in order, skipping WSL launchers under `System32`, `Sysnative`, and `%LOCALAPPDATA%\Microsoft\WindowsApps`; confirm a candidate reports `$OSTYPE` beginning with `msys` or `cygwin`. If none qualifies, locate Git with `shutil.which("git")` and try `<Git install>\bin\bash.exe`, with the same validation. Fail clearly if no Git Bash/Cygwin Bash is available. On non-Windows, use the normal Bash lookup. Preflight `jq` and `curl` in the selected Bash environment. Send code to `execute-code.sh` using its `-`/stdin form, never `-c`, to avoid Windows command-line limits. Suppress bytecode writing for the manager/test process.

Generate a worker password file. Launch `<ParentRuntime.python_executable> -m marimo -q edit <notebook> --headless --host 127.0.0.1 --port <port> --token-password-file <path> --no-skew-protection --no-sandbox --skip-update-check` (`-q` is a global option and must precede `edit`) in `cwd=ParentRuntime.cwd`, without `--session-ttl`. The valid worker script header carries effective absolute `runtime.pythonpath` and `runtime.dotenv` paths; the worker directory remains in private temp. Choose an available loopback port and retry bind conflicts. Use `start_new_session=True` on POSIX and `CREATE_NEW_PROCESS_GROUP` on Windows. Build a child environment with required runtime paths but remove the parent `MARIMO_TOKEN`; set `PYTHONDONTWRITEBYTECODE=1`, `stdin=DEVNULL`, stdout/stderr to a worker log file, and `close_fds=True`; never pass the parent token in the server or kernel environment. Use `-q` to suppress the startup URL; before displaying any log tail, redact exact token values and all `access_token=` query values. Include only a redacted tail in startup failures. The integration assertion must ensure the raw worker log contains no worker token.

Write a provisional `starting` record before spawn and update it with PID/process identity immediately after `Popen`; mark `running` only after validated readiness. A brand-new headless server has no session, so first verify process identity, verify unauthenticated `/api/sessions` is rejected, and verify the authenticated session list is empty. Connect to `/sse` with an explicit new session ID and bearer token, omitting `file` for the single-file worker. Parse SSE records as JSON; fail fast on `event: close`, `kernel-startup-error`, invalid auth, timeout, or EOF before `kernel-ready`. After connection, require authenticated `/api/sessions` to contain the expected session and normalized real worker notebook path. Do not treat `/health` as identity proof.

Before every later SSE, execution, and shutdown request, verify the recorded child process identity, that unauthenticated `/api/sessions` remains rejected, and that authenticated `/api/sessions` still maps the expected session to the recorded worker path. If any check fails, mark/discard the worker as lost and do not contact a possibly reused port. After readiness, verify HTTP execution after closing SSE. If disconnect makes the session unusable, retain an SSE drainer that actively discards events until shutdown; never leave an unbounded queue.

Run the required first worker command `import marimo._code_mode as cm; help(cm)` and verify success. Before handoff, query the worker's effective config with `get_default_config_manager(current_path=worker_notebook).get_config(hide_secrets=False)` and verify its absolute `runtime.pythonpath`/`runtime.dotenv` paths match the captured parent paths; verify its interpreter/version/prefix identity via `verify_runtime_environment`, and verify each import root is active in `sys.path`. On mismatch, stop the worker and fail startup. In a later call create/run the bootstrap cell with `ctx.create_cell` and `ctx.run_cell`, then verify all requested names. Delete the private state artifact after success. On stop, revalidate identity/session, POST `/api/kernel/shutdown` with bearer and session headers, treat non-2xx as failure, stop the optional drainer, and wait up to 10 seconds. Integration must prove graceful exit without forced kill. Only after timeout, recheck process identity before a POSIX process-group kill or Windows `taskkill /T /PID`; never use `os.kill(pid, 0)` on Windows. Use Windows `OpenProcess`/`GetProcessTimes`/`WaitForSingleObject`; on Linux verify `/proc/<pid>/stat` start time, and on macOS verify `ps` start time plus executable before waiting/killing. Retry temp-directory deletion on Windows after process exit. Let `list`/`stop` recover or clean stale `starting` and dead records. Clean token/artifact/log/temp data on startup failure, cancellation, and stop.

- [ ] **Step 4: Run lifecycle tests and the initial integration test**

Run the unit test command from Step 2. Expected: PASS for process arguments/logging, Bash preflight, SSE readiness/error handling, port/session identity, safe shutdown, cross-process cleanup, and platform wait behavior.

Run the opt-in integration suite. Expected: project, notebook-adjacent `.env`, and script-header config are restored into workers from private temp notebooks; two independent workers remain isolated; parent values/source/marker are unchanged; shutdown is graceful without force-kill.

- [ ] **Step 5: Commit the worker lifecycle**

```bash
git add skills/marimo-pair/scripts/branch.py tests/test_branch.py tests/test_branch_integration.py
git commit -m "feat: manage isolated marimo branch workers"
```

## Task 3: Add restricted named exports and explicit branch routing

**Files:**
- Modify: `skills/marimo-pair/scripts/branch.py`
- Modify: `skills/marimo-pair/scripts/execute-code.sh`
- Modify: `tests/test_branch.py`
- Modify: `tests/test_branch_integration.py`

**CLI:**
- `branch.py start --parent-url URL --parent-session ID --names NAME...`
- `branch.py add --worker ID --parent-session CURRENT_ID --names NAME...` (coordinator only; never run directly by a branch agent)
- `branch.py run --worker ID -` (code on stdin)
- `branch.py list` (coordinator only; no token values)
- `branch.py stop --worker ID` (coordinator only)

The worker record stores the parent URL and canonical notebook path privately. `add` requires a fresh parent session ID because browser reconnects can rename sessions; validate `/api/sessions` maps that ID to the same real parent notebook path before exporting. A branch agent can send a names-only add request through the coordinator, but the coordinator alone invokes `branch.py add` and runs the fixed exporter with the parent token. Do not expose the `add` command to the branch agent or place the parent token in its environment. The agent receives only worker-specific `run` capability and never the parent URL/token or a generic parent executor.

- [ ] **Step 1: Write failing add/routing tests before implementing the commands**

Test that `run --worker ID` always supplies the recorded worker URL/session and worker token, never accepts an arbitrary URL, and sends code via stdin. Exercise `execute-code.sh` against a local HTTP fixture with a UTF-8 byte-mode stdin payload of at least 2 MiB containing non-ASCII text; verify the exact JSON code body arrives (proving source/body data did not pass as a process argument). With the same fixture, verify `-c CODE`, a file argument, and stdin without `-` send the expected body; `--session` sends `Marimo-Session-Id` without requesting `/api/sessions`; `--file` and no selector resolve via `/api/sessions`; `MARIMO_TOKEN` supplies the bearer header; a `done` event with `success: false` exits 1; and a stream missing `done` exits nonzero. Test `add` with a later parent value to prove it snapshots the parent's current state and prints the required current-state warning. Test collisions with imported names, names defined by agent cells, and all leading-underscore names; reject before parent export. Test that a failed restore deletes its newly created bootstrap cell and artifacts, leaves prior worker cells usable, and that two successful batches use unique private helper names. Test stale parent session/path mismatch rejection and alias preservation within one batch but not across batches. Assert tokens never appear in `branch.py` arguments, logs, registry, or worker-agent environment. Explicitly note that the existing `execute-code.sh` passes the bearer header in `curl`'s argv; do not claim the token is absent from every child process argv.

Extend the integration test with a later new parent variable, add it to one worker, and assert its value and warning. Define a same-named worker variable and assert collision rejection. Attempt to export a non-pickleable parent lambda and assert a name/type error, no partial bootstrap cell, and continued usability of the existing worker.

- [ ] **Step 2: Run routing/add tests and confirm they fail**

Run `python -B -m unittest discover -s tests -p "test_branch.py" -v` on Windows PowerShell/Git Bash or `python3 -B -m unittest discover -s tests -p "test_branch.py" -v` on macOS/Linux.
Expected: FAIL for the absent CLI, add behavior, and route checks.

- [ ] **Step 3: Implement the CLI and named-only export path**

Validate names (reject every leading-underscore name) and current worker graph definitions before any parent request. The branch agent sends only a names-only add request to its coordinator; the coordinator invokes `branch.py add` using the stored parent URL plus a supplied current session ID. The CLI never accepts source code or an arbitrary parent URL. After path validation, update the worker record to the fresh parent session ID for the next batch. Generate fixed parent-export code that accepts names and artifact paths only. On successful export, build a unique worker bootstrap cell and verify requested names. If bootstrap execution fails, call `ctx.delete_cell` for that batch, remove its artifacts, and leave earlier worker cells intact. Every later batch prints that it reflects the parent's current state; aliases across batches are not preserved.

Update `execute-code.sh` so every input mode uses one request path: feed stdin/file bytes directly, or `-c` via shell-builtin `printf '%s'`, through `jq -Rs '{code: .}' | curl --data-binary @-`. No pre-request step may consume inherited stdin. Do not pass source or encoded body in `jq`/`curl` arguments. Preserve existing CLI options, authentication header, session resolution, SSE success/failure, and ordinary coordinator pairing behavior. Implement `run` by resolving only the worker ID and calling `execute-code.sh` with explicit worker URL/session and token environment. Implement start/list/stop/add for the coordinator; never expose `add` directly to the branch agent. Store one registry file per worker so concurrent starts cannot overwrite one another. Do not call `discover-servers.sh` for worker selection. Use `Bash(python **/scripts/branch.py *)` and `Bash(python3 **/scripts/branch.py *)` patterns where the skill requires them.

- [ ] **Step 4: Run routing/add unit tests and the extended integration test**

Run: `python -B -m unittest discover -s tests -v` on Windows PowerShell/Git Bash or `python3 -B -m unittest discover -s tests -v` on macOS/Linux. Expected: PASS; explicit routing, stale-session detection, current-state warning, collisions, failed-batch rollback, and prior-worker usability are covered.

Run the opt-in integration suite and verify the later-batch scenario against a real marimo server. Also run one CLI-level scenario with `subprocess.run(..., capture_output=True, timeout=...)`: invoke `branch.py start`, assert it returns promptly and the worker remains alive after the start process exits, then invoke `run`, coordinator-side `add`, and `stop` in separate processes. Verify cross-process cleanup and that captured pipes do not prevent start from returning (including on Windows).

- [ ] **Step 5: Commit the branch command interface**

```bash
git add skills/marimo-pair/scripts/branch.py skills/marimo-pair/scripts/execute-code.sh tests/test_branch.py tests/test_branch_integration.py
git commit -m "feat: add branch and explore commands"
```

## Task 4: Document worker permissions and workflow

**Files:**
- Modify: `skills/marimo-pair/SKILL.md`
- Modify: `skills/marimo-pair/reference/execution-context.md`

- [ ] **Step 1: Document commands and the tool-permission boundary**

Keep direct `execute-code.sh` and server discovery for ordinary coordinator pairing. Add coordinator-side `Bash(python **/scripts/branch.py *)` and `Bash(python3 **/scripts/branch.py *)` forms. Explain that a branch agent receives only worker-specific execution; it sends a names-only add request as a coordinator message, and the coordinator runs `branch.py add` with parent credentials. Do not give the worker agent the `add` command, direct execute/discovery commands, the parent URL, or the parent token. Keep start/list/stop/add with the coordinator. State explicitly that `allowed-tools` on the shared skill is not per-agent isolation: the boundary depends on coordinator handoff and harness-level tool permissions.

- [ ] **Step 2: Document export, refresh, and failure behavior**

Explain current-state snapshots, fresh parent session IDs for additional exports, collision/restart guidance, local-host/context limits, explicit worker IDs, readiness verification, no session TTL, safe cleanup, and lost-worker behavior. Clarify the parent remains ordinary edit mode and isn't globally read-only.

- [ ] **Step 3: Read both docs in full and verify instructions agree**

Confirm normal single-kernel pairing still works, branch workers receive only worker-specific run capability and can only request names-only exports through the coordinator, and no worker instruction selects a server from global discovery.

- [ ] **Step 4: Commit workflow documentation**

```bash
git add skills/marimo-pair/SKILL.md skills/marimo-pair/reference/execution-context.md
git commit -m "docs: describe marimo-pair branch workers"
```

## Task 5: Run acceptance checks and finalize

**Files:** Verify all three changed scripts (`branch_state.py`, `branch.py`, `execute-code.sh`), all tests/fixtures, and the two skill docs listed above.

- [ ] **Step 1: Run all unit tests without bytecode artifacts**

Run: `python -B -m unittest discover -s tests -v` on Windows PowerShell/Git Bash or `python3 -B -m unittest discover -s tests -v` on macOS/Linux.
Expected: PASS; opt-in integration tests skip when disabled, and no repository `__pycache__` is created.

- [ ] **Step 2: Run the opt-in integration suite**

Git Bash: `MARIMO_PAIR_RUN_INTEGRATION=1 python -B -m unittest tests.test_branch_integration -v`

PowerShell: `$env:MARIMO_PAIR_RUN_INTEGRATION = "1"; python -B -m unittest tests.test_branch_integration -v`

macOS/Linux: `MARIMO_PAIR_RUN_INTEGRATION=1 python3 -B -m unittest tests.test_branch_integration -v`

Expected: parent fixture auto-runs exactly once via explicit instantiate and authenticated status-plus-marker check; two workers execute concurrently and remain isolated; later coordinator-mediated export works; project, notebook-adjacent, and script-header pythonpath/dotenv restore works from private temp workers; worker logs contain no token; worker shutdown exits gracefully without force; source and marker are unchanged after exploration.

- [ ] **Step 3: Verify acceptance criteria and worker tool permissions**

Map every spec acceptance criterion to a named test. Confirm startup doesn't replay parent cells, all routes use explicit session IDs and tokens, parent exports take names only, parent session/path is revalidated on coordinator-side add, SSE errors fail fast, no-token workers do not enter discovery, and branch agents get only run capability plus a coordinator-mediated names-only export request.

- [ ] **Step 4: Check repository output and platform limits**

Run: `git diff --check 49a205a..HEAD && git status --short --branch`
Expected: no whitespace errors; no unintended `__pycache__`; the untracked `doc/` source dump remains unstaged and uncommitted. Report platform validations unavailable on the current Windows host; do not overclaim POSIX/macOS coverage.

## Completion Gate

Implementation remains blocked until a reviewer approves this plan. After approval, implement task-by-task with review checkpoints. Before marking implementation complete, run the full Windows suite and opt-in integration test, report unavailable POSIX/macOS checks, and have a reviewer inspect explicit routing and cleanup. Do not modify upstream marimo or stage the supplied `doc/` source dump.
