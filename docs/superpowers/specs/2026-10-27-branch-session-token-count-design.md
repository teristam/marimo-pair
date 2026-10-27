# Branch Workers: Session Resolution, Token Files, and Multi-Worker Start

## Status

Design approved in conversation 2026-10-27; awaiting written-spec review. Addresses fork issues teristam/marimo-pair#2, #3, #4 (and incidentally #7, the docs part of #9, and part of #10). All changes stay in marimo-pair; upstream marimo is not modified.

## Goal

Remove three sources of coordinator friction observed when starting three parallel branch workers:

1. **#2** The coordinator had to discover the parent session key by hand (via a separate authenticated `curl`) and guess which of two ID-like values `--parent-session` wanted.
2. **#3** Starting N workers with the same values exported (pickled) the parent N times.
3. **#4** Tokens could only be supplied via `MARIMO_TOKEN` or `--token`, and `execute-code.sh` put the bearer header in `curl` argv.

Success criteria:

- A coordinator can start and extend workers given only the parent URL and (optionally) the notebook path; no manual `/api/sessions` lookup.
- `start --count N` serializes parent values exactly once and returns N workers, or none.
- A token can be supplied as a file; no bearer token appears in any `curl` argv from `execute-code.sh` (parent or worker).
- All existing safeguards and tests keep passing.

## Invariants (unchanged)

Authenticated-only parent (unauthenticated `/api/sessions` must be rejected); loopback-only parent URL; parent notebook canonical-path cross-check and OS path boundary check; parent export accepts names only; per-worker tokens via `--token-password-file`; `run` routes only by worker ID with code on stdin; branch agents never receive the parent token or coordinator subcommands; batch rollback semantics for `add`.

## 1. Parent session resolution (#2)

### `WorkerManager.resolve_parent(url, *, notebook=None, session=None, token) -> ParentTarget`

Replaces `parent_target()`. Fetches the authenticated `/api/sessions` mapping once, then:

1. **`session` given:** match listing keys first, then each entry's `"session_id"` field. The result is always the listing **key** (what `Marimo-Session-Id` / `execute-code.sh --session` use). If `notebook` is also given, the matched entry's canonical path must equal it.
2. **Only `notebook` given:** canonical-path match (`_real_path`, `_same_notebook_path`) over all entries. Exactly one match wins. Zero: `BranchError("no active parent session for <path>")`. Several: `BranchError("<n> sessions are open for <path>: <keys>; pass --parent-session")`.
3. **Neither:** exactly one open session is used. Otherwise `BranchError` listing each `key → path`.

`_validate_parent` then runs unchanged on the resolved target.

### CLI

- `start --parent-url URL [--parent-notebook PATH] [--parent-session KEY_OR_ID]`
- `add --worker ID [--parent-session KEY_OR_ID]`. By default `add` resolves with `notebook=record.parent_notebook_path`, so the current session is found after browser reconnects. An explicit session must still map to the worker's original notebook (existing check).
- `start` output includes `"parent_session": <key>`.

## 2. Multi-worker start (#3)

### `WorkerManager.start_workers(parent, names, count) -> list[WorkerRecord]`

`start_worker(parent, names)` becomes `start_workers(parent, names, 1)[0]`.

1. Validate names and the parent once.
2. Create worker 1's record and temp dir as today; **export once** into it (`values.pickle`, `runtime.json`); read runtime metadata and cross-check the notebook path once.
3. For k = 1..N sequentially: create record/dir (k > 1), set `bundle_path` and `runtime_path` to worker 1's shared artifacts, copy its own helper, write its own `worker.py`, then launch and initialize (runtime check, bootstrap, name verification) as today.
4. After all N are running: delete the shared bundle and runtime metadata and every helper copy; mark records `running`.

The shared bundle lives in worker 1's temp dir so that an interrupted `start` is recovered by the existing `list`/`stop` path (cleaning worker 1's dir removes it). No new record type is introduced. Launch is sequential: the parent-blocking export happens once; launches do not touch the parent. Parallel launch is out of scope.

### Failure semantics: all-or-nothing, startup only

Any error during `start_workers` (parent validation, export, any launch, any runtime check or bootstrap, the CLI drainer check) stops every worker created so far in reverse order via the existing abort/stop path, removes shared artifacts, and re-raises identifying the failing worker index. If any cleanup fails, the error lists the worker IDs still registered for recovery (mirrors today's single-worker message). `read_batch` deleting the bundle on a restore error is acceptable because the batch aborts.

Once `start` returns, workers are independent: a failing or crashing `run` affects only that worker; nothing cascades or rolls back. A replacement worker (`start --count 1`) is a fresh snapshot of current parent state.

### CLI

- `start ... --count N`, default 1, range 1–16 (argparse error outside).
- Without `--count`: the current single JSON object (backward compatible), plus `parent_session`.
- With `--count`: a JSON list of those objects.

All N workers share one snapshot; docs state this.

## 3. Token sources and header transport (#4)

### Sources (both scripts), highest precedence first

1. `--token TOKEN` — still accepted; prints a stderr warning that it exposes the token in process arguments.
2. `--token-file PATH`
3. `MARIMO_TOKEN_FILE`
4. `MARIMO_TOKEN`

Token files are read without spawning processes (bash `IFS= read -r`; Python `Path.read_text`), trailing CR/LF stripped. Errors: unreadable file, empty token, or any control character in the token (prevents header injection).

### `execute-code.sh`

- When a token is present: create a header file with `umask 077` + `mktemp "${TMPDIR:-/tmp}/marimo-pair-auth.XXXXXX"`, write `Authorization: Bearer <token>` with `printf` (builtin), remove it via `trap` on EXIT/INT/TERM/HUP.
- Both `curl` calls (`/api/sessions` and `/api/kernel/execute`) use `-H @"$hdr"`. No token in argv.
- Without a token: no header file; behaviour unchanged.
- Require curl ≥ 7.55 (`-H @file`) when a token is present; otherwise fail with a clear message. No fallback to argv.

### `branch.py`

- `_parent_token(token_file: str | None) -> str` implements the precedence above (minus `--token`, which `branch.py` does not offer) and replaces the direct `os.environ["MARIMO_TOKEN"]` reads in `parent_target`/`_validate_parent`/`_export_parent`.
- `start` and `add` gain `--token-file`.
- The child `execute-code.sh` continues to receive the token via `MARIMO_TOKEN` in its environment (owner-readable only); the header file keeps it out of `curl` argv. Worker tokens use the same path, so `run` also stops exposing its worker token.
- Redaction uses the resolved parent token, not only the env var.
- The resolved token is never written into worker records, temp dirs, or worker environments (worker server env still drops `MARIMO_TOKEN`/`MARIMO_TOKEN_FILE`).

## Documentation

- SKILL.md, `reference/execution-context.md`, `reference/finding-marimo.md`: recommend `MARIMO_TOKEN_FILE` / `--token-file`; remove the "bearer header in transient curl argv" caveats; keep same-user/not-a-sandbox warnings.
- Replace the "obtain session ID from `/api/sessions` via separately approved HTTP tooling" step and the `add` reconnect warning with `--parent-notebook` usage; note that `localhost` is accepted.
- Replace the multi-start shell loop with `start --count N`; state that the N workers share one snapshot and that startup is all-or-nothing.
- Add argparse `help=` strings for new and changed flags.
- Keep SKILL.md under 500 lines.

## Testing (TDD)

- `tests/test_execute_code_transport.py`: an argv-logging `curl` shim on `PATH` that execs the real curl against the existing fixture server. Assert the fixture receives the right `Authorization` header and the token never appears in logged argv; header file removed after success, failure, and SIGINT; source precedence; `--token` warning; CR/LF and empty token rejection; curl version gate (shimmed `--version`).
- `tests/test_branch.py` (stubbed HTTP/export/launch): resolution by key, by `session_id`, by notebook, ambiguous, none, single default, session+notebook mismatch; `add` auto-resolve after a key change; `start_workers` calls the export once for N; failure at worker k aborts workers 1..k−1 and removes shared artifacts; cleanup-failure message lists IDs; `--count` bounds and output shapes; `_parent_token` precedence and errors; redaction of a file-sourced token.
- `tests/test_branch_integration.py` (gated by `MARIMO_PAIR_RUN_INTEGRATION=1`): `start --count 2 --parent-notebook ... --token-file ...`; both workers execute; both stop cleanly.
- Run the full suite before claiming completion.

## Out of scope

Parallel worker launch; persistent/reusable bundles (`export` + `start --bundle`); bringing results back into the parent (#1); run-only worker capability (#5); worker persistence (#6).
