# marimo-pair Branch-and-Explore Design

## Status

Design proposal for review. It authorizes no implementation. All feature changes must remain in marimo-pair; upstream marimo source and APIs are not to be modified.

## Goal and scope

Let a small number of agents explore different hypotheses in parallel using selected values already loaded in the user's live marimo kernel. Each agent gets its own mutable copy of requested values, avoiding expensive preprocessing and preventing mutations from crossing between the parent and workers. Agents inspect the original notebook, request additional values as needed, and report promising results. Worker changes are not automatically merged into the original notebook.

V1 is local-host only. It creates one temporary notebook, marimo server, and session per agent. It does not clone the live Python process, copy the original cell graph, or rerun the original notebook.

## Architecture and data flow

1. **Inspect:** The agent reads the original notebook and asks for the named variables needed for its hypothesis.
2. **Export:** A restricted marimo-pair helper executes only a named-variable export operation in the parent scratchpad. It serializes one requested batch into a private worker artifact and records the parent context needed for restore: working directory, `sys.path`, Python executable/environment, and relevant marimo sandbox/project configuration. The helper does not accept arbitrary agent code.
3. **Start worker:** marimo-pair creates a unique temporary directory and worker notebook, then starts a separate local marimo server for that agent. It uses the parent's Python environment and working/project context where possible. It never opens the original notebook path as the worker notebook.
4. **Open worker session:** marimo-pair makes a fresh connection to that worker server, independent of the parent connection. A headless server alone does not create a kernel session. The existing `/sse` endpoint can be used as an experimental client transport: open it, actively drain messages until the worker reports ready, then close it. Do not set a session TTL; verify the edit-mode session remains usable over HTTP after this disconnect. If it does not, use a normal worker UI/WebSocket connection or a continuously drained SSE connection. Do not add upstream session-creation APIs.
5. **Bootstrap:** Once the worker kernel is ready, marimo-pair explicitly creates and runs a worker-local bootstrap cell using the existing code-mode API (`ctx.create_cell` then `ctx.run_cell`). Do not rely on edit-mode startup or a bare SSE connection to execute notebook cells. Generate explicit assignments for each transferred name so marimo registers those definitions; do not use a dynamic `globals().update(...)`. Verify every requested name is bound before handing the worker to the agent. This is a minimal graph for the worker only, not a copy of the original graph.
6. **Explore:** The agent tests hypotheses in its isolated worker and reports findings. If it needs more variables, it requests another export from the parent; marimo-pair adds and runs a bootstrap cell for that batch without replacing the worker's existing state. Reject names already defined in the worker. Aliases between separate batches are not preserved; to refresh a value or preserve aliases across batches, restart the branch and export the needed names together.
7. **Cleanup:** When the agent finishes or is cancelled, call marimo's existing kernel shutdown endpoint, then stop the worker server and delete its temporary notebook and state artifacts, unless the user explicitly asks to retain the branch. Do not rely on disconnect or session TTL for cleanup; use force termination only as a last resort.

## Worker connection and routing

- Bind each worker server to loopback and assign a unique worker ID, URL, session ID, token, directory, and notebook path.
- Store the mapping in a marimo-pair-owned worker registry. Do not use global server discovery to select a worker.
- Every worker execution must pass its worker URL and session ID explicitly. Never auto-select a session or route a worker command to the parent URL.
- Agents receive a worker-specific execute helper and a restricted parent export helper; they do not receive a general-purpose parent execute route. Parent export accepts variable names only, not Python source.
- Use per-worker authentication and keep tokens in the coordinator, not in notebook code or serialized artifacts. Supply a generated per-worker password through `--token-password-file`; avoid `--no-token` because those servers register in global discovery. If no-token mode is unavoidable, filter worker entries from general discovery and still require explicit worker routing.
- Update marimo-pair's skill/tool guidance so it distinguishes parent inspection/export from worker execution and does not tell worker agents to choose a server from the global discovery list.

The supplied marimo source includes an experimental `/sse` endpoint. The endpoint is registered independently of the browser transport setting; `server.transport` controls which transport the browser uses, not whether marimo-pair can call the route. Drain the stream through the ready notification, then close it and verify the worker session remains usable; do not set a session TTL. This avoids a long-lived drainer and its unbounded message queue. Since SSE is experimental, validate the installed version. If a closed stream ends the session, use a normal worker UI/WebSocket client or keep a client actively draining SSE through worker shutdown, without changing upstream marimo.

## Serialization and restore contract

- Export only agent-requested names that are valid Python identifiers, not every entry in Python `globals()` and not the entire running process. Pass names as data to a fixed export helper. For the worker bootstrap, emit only validated identifier tokens in explicit assignments; never treat arbitrary name text as code or evaluate it.
- Serialize each requested name as a record in one batch using a shared pickler/unpickler memo, preserving aliases among names in that batch and attributing failures to the current name. Abort and delete the entire batch artifact on any failure. Verify the chosen serializer's repeated-record memo behavior before implementation.
- V1 supports values that can be serialized and restored in the same Python environment. Reject notebook `__main__` functions/classes, marimo UI elements, `mo.state`/runtime-context values, and other unsupported objects with an actionable name/type error. Inspect supported nested object graphs for unsupported `__main__` references; checking only top-level values is insufficient. Where relevant, suggest exporting an underlying plain value (for example a UI element's `.value`) instead. Do not silently substitute a same-named worker object.
- Before unpickling, restore the parent's working directory and `sys.path`; use the same Python executable/environment, project configuration (including `pythonpath` and dotenv settings), and sandbox dependencies so local-module classes resolve consistently. If that context cannot be reproduced, fail before handing the worker to the agent.
- The worker bootstrap cell assigns names explicitly so marimo sees their definitions. A scratchpad-level `globals().update(...)` is not a restore mechanism: scratchpad bindings are temporary and are not registered as cell definitions.
- Additional batches are independent transfers. Create and run a new bootstrap cell for new names, but reject names already defined in the worker, including names created by agent cells. To refresh a value or preserve aliasing with earlier values, restart the branch and export the needed names together. Aliases within one batch should be retained.
- This is a value snapshot, not a clone of process-wide state. Modules, open files, database connections, locks, active threads/tasks, GPU/device handles, and external resources are not guaranteed to become independent worker state. Such values need a future explicit adapter or must be recreated by the agent.
- Python pickle loading can execute code. Artifacts are private, local, and trusted-only; delete them at cleanup.

## Parent consistency and isolation

- The parent is used by marimo-pair agents only for notebook inspection and named export; hypothesis code runs only in workers. Exporting may invoke user-defined serialization hooks, so the parent should be idle during export. Do not give worker agents arbitrary parent-session execution access.
- The parent remains an ordinary edit-mode server. marimo-pair can prevent its agents from executing against the parent through scoped helpers, but cannot make the entire server read-only to a browser user or another client without upstream changes.
- Each export reflects the parent values at the time of that export. marimo-pair cannot detect arbitrary in-place parent mutations reliably. Always warn that later batches may reflect newer parent state; for a consistent branch, keep the parent idle or restart the branch after parent changes.
- Each worker has a unique temporary notebook path, preventing edit-mode file-key reuse and autosaves from overwriting the original notebook. Workers use separate server/kernel processes; mutating a successfully restored in-memory object in one worker must not mutate the parent or another worker.
- Workers do not receive the original notebook graph or execute its preprocessing cells. Agents inspect original source through the parent path and explore with the transferred values plus worker-local code.
- This design is local-host only for V1 because the parent kernel and worker processes must access the same temporary artifacts. Remote parent/worker transfer is out of scope.

## Alternatives considered

- **Fork/copy-on-write the live kernel:** Rejected. It is POSIX-only, unavailable on Windows, and unsafe forking a live marimo/Python process with event loops, threads, locks, and IPC.
- **Copy and replay the original notebook:** Rejected for the primary flow because it repeats expensive preprocessing and does not preserve live-only mutations.
- **marimo persistent cache:** A fallback for deterministic preprocessing, but it re-executes notebook code and is not a snapshot of the current live values.
- **Upstream marimo branch/session API:** Explicitly out of scope; all changes must stay in marimo-pair.

## Failure behavior

- If worker startup, SSE/WebSocket connection, readiness, or bootstrap execution fails, report the worker ID and error, and clean up its server and files.
- If a requested value cannot be serialized or restored, report its name/type and leave the parent and other workers unchanged. Do not hand a partially initialized worker to an agent.
- If a requested name already exists in the worker, reject the addition with guidance to choose a new name or restart the branch.
- Every later export batch must warn that it reflects the parent's current state. In-place parent-state drift cannot be detected reliably; restart the branch when a consistent snapshot is required.
- If a worker server is lost, discard the branch rather than reconnecting its agent to the parent.

## Acceptance criteria

1. Two workers start from one parent session and execute independently in parallel, each on a distinct server/session and temporary notebook.
2. Worker and parent helper commands always use explicit URLs and session IDs, plus a token whenever that server requires one; neither direction can be accidentally routed to the other process. Worker discovery does not use the global server list.
3. A worker session is explicitly connected and ready before bootstrap. Its generated bootstrap cell is explicitly run, and the agent receives the worker only after all requested names are verified present.
4. Worker startup does not rerun the original preprocessing cells. The original notebook source, graph, and successfully transferred parent values remain unchanged when a worker mutates its copy.
5. The worker restores required working-directory, import-path, Python environment, and project-config context sufficiently to load a representative local-module data class.
6. One batch preserves aliases among supported values; unsupported values fail with an attributable name/type error and no partial branch handoff.
7. An agent can request a new, non-conflicting variable batch and continue in its worker; name collisions are rejected and every later batch carries a warning that it reflects the parent's current state.
8. Worker session, kernel, server, notebook, token metadata, and artifacts are cleaned up on success, error, and cancellation, including on Windows.
9. The feature changes only marimo-pair files and requires no source changes to upstream marimo.

## Known risks to verify before implementation

- The source labels SSE experimental. Verify that after draining `/sse` through readiness and closing it, the session remains usable by HTTP execution with no session TTL. Otherwise use a browser/WebSocket client or a continuously draining SSE client. Use the existing shutdown endpoint rather than relying on TTL.
- Confirm installed-version CLI flags, token handling, transport behavior, and sandbox/config reproduction.
- Verify shared-memo serialization and local-module restore before choosing a stable bundle format.
- Ensure the skill/tool interface prevents global server discovery from routing an agent to the wrong process.
