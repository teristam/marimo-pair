# Branch Session Resolution, Token Files, and Multi-Worker Start Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a coordinator start N branch workers from one parent export using only a parent URL/notebook path and a token file, with no bearer token in any `curl` argv.

**Architecture:** `execute-code.sh` resolves its token from flag/file/env and hands curl a private header file (`-H @file`). `branch.py` gains a single parent-token resolver, a `resolve_parent()` session lookup (key, `session_id` field, notebook path, or lone session), and `start_workers()` which exports once into worker 1's private dir and bootstraps N workers from it, all-or-nothing.

**Tech Stack:** Bash (`execute-code.sh`, needs `jq`, `curl` ≥ 7.55), Python 3.10+ standard library (`unittest`, `argparse`, `subprocess`), marimo `_code_mode`.

**Spec:** `docs/superpowers/specs/2026-10-27-branch-session-token-count-design.md`

## Global Constraints

- All changes stay in marimo-pair (`skills/marimo-pair/**`, `tests/**`, `docs/**`); upstream marimo is not modified.
- Token precedence (highest first): `--token TOKEN` (execute-code.sh only, warns) > `--token-file PATH` > `MARIMO_TOKEN_FILE` > `MARIMO_TOKEN`.
- Token files: trailing CR/LF stripped; empty token, unreadable file, or any control character → error before any HTTP request.
- With a token, `execute-code.sh` requires curl ≥ 7.55 and never falls back to putting the header in argv.
- `start --count N`: default 1, range 1–16. Without `--count` print one JSON object; with `--count` print a JSON list. Each object has `worker_id`, `url`, `session_id`, `status`, `parent_session`.
- Startup is all-or-nothing; runtime (`run`) failures never cascade.
- Preserve every existing invariant: authenticated-only loopback parent, notebook canonical-path cross-check, names-only parent export, per-worker tokens, `run` routes only by worker ID with stdin, branch agents never get the parent token.
- Child processes that receive a worker or parent token via `MARIMO_TOKEN` must have `MARIMO_TOKEN_FILE` removed from their environment (it outranks `MARIMO_TOKEN`).
- SKILL.md stays under 500 lines.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**Test commands (Linux/macOS):** `python3 -B -m unittest tests.test_execute_code_transport tests.test_branch tests.test_branch_state -v`. Integration: `MARIMO_PAIR_RUN_INTEGRATION=1 python3 -B -m unittest tests.test_branch_integration -v` (needs the marimo venv: `tests/notebooks/.venv/bin/python -B -m unittest ...`).

**Baseline:** on Linux the unit suite currently has 3 pre-existing failures, all Windows-simulation tests unrelated to this work: `test_first_valid_windows_candidate_wins_after_skipping_wsl_launchers`, `test_override_is_first_but_must_be_validated`, `test_windows_stop_cleans_up_after_taskkill_and_signaled_handle`. "Pass" below means no failures other than these three.

## Review Focus

1. **Coordinator has `MARIMO_TOKEN_FILE` set while `branch.py` passes a *worker* token via `MARIMO_TOKEN`** — the child `execute-code.sh` must use the worker token, not re-read the parent file. Pinned in Task 2.
2. **`add` on worker k>1 after `start --count`** — its `bundle_path`/`runtime_path` must point into its own temp dir, not worker 1's (which may be stopped, or racing another `add`). Pinned in Task 4 (unit) and Task 5 (integration).
3. **`--parent-notebook` given as a relative path or a symlink** — must match the server's absolute path for the same file. Pinned in Task 3.
4. **Token files written with `echo token > file` or a Windows editor (trailing `\n` / `\r\n`)** — must work, while a second line must be rejected. Pinned in Tasks 1 and 2.
5. **Lone open session with no notebook path (unsaved/new notebook)** — auto-selection must fail with a clear message, not a traceback. Pinned in Task 3.

---

### Task 1: `execute-code.sh` token sources and header-file transport

**Files:**
- Modify: `skills/marimo-pair/scripts/execute-code.sh` (header comment lines 13-14, `usage` line 22, variables/parsing lines 32-48, new token block after the input-file check ~line 81, version gate after the tool check ~line 90, auth block lines 122-126)
- Test: `tests/test_execute_code_transport.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `execute-code.sh` accepts `--token-file PATH` and env `MARIMO_TOKEN_FILE`; sends `Authorization` via `curl -H @<private file>`. Tasks 2–5 rely on the child honouring `MARIMO_TOKEN` when `MARIMO_TOKEN_FILE` is absent.

- [ ] **Step 1: Add the curl shim and env plumbing to the test fixture**

In `tests/test_execute_code_transport.py`, add `import signal` and `import time` to the imports. Add a `hang` mode to `_FixtureHandler.do_POST` right after the stdout event is written:

```python
        self.wfile.write(b"event: stdout" + nl + b'data: {"data":"ok"}' + nl + nl)
        if mode == "hang":
            self.wfile.flush()
            time.sleep(5)
            return
```

Add a module-level shim source above the test class:

```python
CURL_SHIM = """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  if [[ -n "${FAKE_CURL_VERSION:-}" ]]; then
    echo "curl ${FAKE_CURL_VERSION} (fake)"
    exit 0
  fi
  exec "$REAL_CURL" --version
fi
for arg in "$@"; do printf '%s\\n' "$arg" >> "$CURL_ARGV_LOG"; done
exec "$REAL_CURL" "$@"
"""
```

Replace `setUpClass`, `tearDownClass`, `setUp` and `_run` with:

```python
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.real_curl = shutil.which("curl")
        cls.shim_dir = Path(tempfile.mkdtemp())
        shim = cls.shim_dir / "curl"
        shim.write_text(CURL_SHIM, encoding="utf-8")
        shim.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        shutil.rmtree(cls.shim_dir, ignore_errors=True)

    def setUp(self):
        _FixtureHandler.bodies = []
        _FixtureHandler.headers = []
        _FixtureHandler.session_lookups = 0
        _FixtureHandler.done_mode = "success"
        handle, name = tempfile.mkstemp(prefix="curl-argv-")
        os.close(handle)
        self.argv_log = Path(name)
        self.addCleanup(self.argv_log.unlink, missing_ok=True)

    def _env(self, *, token=True, env=None):
        child_env = os.environ.copy()
        child_env.pop("MARIMO_TOKEN_FILE", None)
        if token:
            child_env["MARIMO_TOKEN"] = "fixture-secret"
        else:
            child_env.pop("MARIMO_TOKEN", None)
        child_env.update({
            "PATH": f"{self.shim_dir}{os.pathsep}{child_env.get('PATH', '')}",
            "CURL_ARGV_LOG": str(self.argv_log),
            "REAL_CURL": self.real_curl,
        })
        child_env.update(env or {})
        return child_env

    def _command(self, args):
        return [str(branch.resolve_bash()), str(SCRIPT), "--url", self.base, *args]

    def _run(self, args, payload=b"", *, token=True, env=None):
        return subprocess.run(
            self._command(args), input=payload, capture_output=True, timeout=30,
            env=self._env(token=token, env=env),
        )

    def _curl_args(self):
        return self.argv_log.read_text(encoding="utf-8").splitlines()

    def _header_files(self):
        return [arg[1:] for arg in self._curl_args() if arg.startswith("@") and arg != "@-"]
```

In `test_large_utf8_byte_stdin_is_streamed_and_session_skips_lookup`, delete the comment line `# The bearer header is still passed in curl's argv; only source and JSON bodies stay off argv.`

- [ ] **Step 2: Write the failing tests**

Append to `ExecuteCodeTransportTests`:

```python
    def test_bearer_token_reaches_server_but_never_curl_argv(self):
        completed = self._run(["-c", "pass"])
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
        self.assertEqual(_FixtureHandler.session_lookups, 1)
        self.assertEqual(_FixtureHandler.headers[-1]["Authorization"], "Bearer fixture-secret")
        args = self._curl_args()
        self.assertFalse(any("fixture-secret" in arg for arg in args), args)
        header_files = self._header_files()
        self.assertEqual(len(header_files), 2, args)
        for path in header_files:
            self.assertFalse(Path(path).exists(), path)

    def test_header_file_removed_after_failed_execution(self):
        _FixtureHandler.done_mode = "failure"
        completed = self._run(["--session", "s", "-c", "pass"])
        self.assertNotEqual(completed.returncode, 0)
        header_files = self._header_files()
        self.assertEqual(len(header_files), 1)
        self.assertFalse(Path(header_files[0]).exists())

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery")
    def test_header_file_removed_when_interrupted(self):
        _FixtureHandler.done_mode = "hang"
        process = subprocess.Popen(
            self._command(["--session", "s", "-c", "pass"]), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(), start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not _FixtureHandler.bodies and time.monotonic() < deadline:
                time.sleep(0.05)
            header_files = self._header_files()
            self.assertEqual(len(header_files), 1)
            os.killpg(process.pid, signal.SIGINT)
            process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(Path(header_files[0]).exists())

    def test_token_sources_follow_flag_then_file_then_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            flag_file = Path(directory) / "flag-token"
            flag_file.write_text("flag-file-secret\n", encoding="utf-8")
            env_file = Path(directory) / "env-token"
            env_file.write_bytes(b"env-file-secret\r\n")
            cases = [
                ([], {}, "fixture-secret"),
                ([], {"MARIMO_TOKEN_FILE": str(env_file)}, "env-file-secret"),
                (["--token-file", str(flag_file)], {"MARIMO_TOKEN_FILE": str(env_file)}, "flag-file-secret"),
                (["--token", "inline-secret", "--token-file", str(flag_file)], {}, "inline-secret"),
            ]
            for extra, env, expected in cases:
                with self.subTest(expected=expected):
                    completed = self._run([*extra, "--session", "s", "-c", "pass"], env=env)
                    self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
                    self.assertEqual(_FixtureHandler.headers[-1]["Authorization"], f"Bearer {expected}")
                    if "--token" in extra:
                        self.assertIn(b"exposes", completed.stderr)
                    else:
                        self.assertNotIn(b"exposes", completed.stderr)
        self.assertFalse(any("secret" in arg for arg in self._curl_args()))

    def test_invalid_tokens_fail_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.write_text("\n", encoding="utf-8")
            multi = Path(directory) / "multi"
            multi.write_text("first\nsecond\n", encoding="utf-8")
            missing = Path(directory) / "missing"
            cases = [
                (["--token-file", str(empty)], {}, b"empty"),
                (["--token-file", str(multi)], {}, b"control"),
                (["--token-file", str(missing)], {}, b"readable"),
                ([], {"MARIMO_TOKEN": "bad\x01secret"}, b"control"),
            ]
            for extra, env, message in cases:
                with self.subTest(message=message, extra=extra):
                    completed = self._run([*extra, "--session", "s", "-c", "pass"], env=env)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(message, completed.stderr.lower())
        self.assertEqual(_FixtureHandler.bodies, [])
        self.assertEqual(self._curl_args(), [])

    def test_without_token_no_header_file_or_authorization(self):
        completed = self._run(["--session", "s", "-c", "pass"], token=False)
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
        self.assertNotIn("Authorization", _FixtureHandler.headers[-1])
        self.assertEqual(self._header_files(), [])

    def test_old_curl_is_refused_only_when_a_token_is_needed(self):
        old = {"FAKE_CURL_VERSION": "7.54.1"}
        completed = self._run(["--session", "s", "-c", "pass"], env=old)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"7.55", completed.stderr)
        self.assertEqual(_FixtureHandler.bodies, [])
        completed = self._run(["--session", "s", "-c", "pass"], token=False, env=old)
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -B -m unittest tests.test_execute_code_transport -v`
Expected: the seven new tests FAIL (token found in argv / `Unknown option: --token-file` / no `7.55` message); the five existing tests PASS.

- [ ] **Step 4: Implement token sources and header file in `execute-code.sh`**

Replace the header comment lines 13-14 with:

```bash
# Auth: set MARIMO_TOKEN_FILE (preferred) or MARIMO_TOKEN, or pass
# --token-file PATH. The bearer header reaches curl through a private
# temporary header file (curl -H @file), never through curl's argv.
# --token TOKEN still works but exposes the token in this script's argv.
```

Replace the `Auth:` usage line with:

```bash
  echo "Auth:  set MARIMO_TOKEN_FILE or MARIMO_TOKEN, or pass --token-file PATH" >&2
```

Replace `token="${MARIMO_TOKEN:-}"` with:

```bash
token=""
token_flag=""
token_flag_set=false
token_file=""
```

and replace the `--token)` case with:

```bash
    --token)      token_flag="$2"; token_flag_set=true; shift 2 ;;
    --token-file) token_file="$2"; shift 2 ;;
```

Immediately after the `Input code file is not a readable regular file` check, insert:

```bash
# Resolve the bearer token: --token, --token-file, MARIMO_TOKEN_FILE, MARIMO_TOKEN.
if [[ "$token_flag_set" == true ]]; then
  echo "Warning: --token exposes the token in process arguments; prefer --token-file or MARIMO_TOKEN_FILE." >&2
  token="$token_flag"
else
  token_file="${token_file:-${MARIMO_TOKEN_FILE:-}}"
  if [[ -n "$token_file" ]]; then
    if [[ ! -f "$token_file" || ! -r "$token_file" ]]; then
      echo "Token file is not a readable regular file: ${token_file}" >&2
      exit 1
    fi
    # $(<file) is read by the shell itself, so no process argv sees the token.
    token=$(<"$token_file")
    token="${token%$'\r'}"
    if [[ -z "$token" ]]; then
      echo "Token file is empty: ${token_file}" >&2
      exit 1
    fi
  else
    token="${MARIMO_TOKEN:-}"
  fi
fi
if [[ "$token" == *[[:cntrl:]]* ]]; then
  echo "The auth token contains control characters; refusing to send it." >&2
  exit 1
fi
```

Immediately after the `execute-code.sh needs ${missing} on PATH.` block, insert:

```bash
# curl reads headers from a file (-H @file) only since 7.55; older versions
# would send the literal "@path" and fail authentication confusingly.
if [[ -n "$token" ]]; then
  curl_version=$(curl --version 2>/dev/null | awk 'NR == 1 { print $2 }') || curl_version=""
  curl_major="${curl_version%%.*}"
  curl_minor="${curl_version#*.}"
  curl_minor="${curl_minor%%.*}"
  if ! [[ "$curl_major" =~ ^[0-9]+$ && "$curl_minor" =~ ^[0-9]+$ ]] \
    || (( curl_major < 7 || (curl_major == 7 && curl_minor < 55) )); then
    echo "execute-code.sh needs curl >= 7.55 to send the auth header from a file (found ${curl_version:-unknown})." >&2
    exit 1
  fi
fi
```

Replace the `# Build optional auth header` block (the `auth_args=()` / `if [[ -n "$token" ]]` lines) with:

```bash
# Hand curl the bearer header through a private file so the token never
# appears in any process's argv. The file is removed on every exit path.
auth_args=()
auth_header_file=""
remove_auth_header() {
  if [[ -n "$auth_header_file" ]]; then
    rm -f -- "$auth_header_file"
  fi
}
if [[ -n "$token" ]]; then
  trap remove_auth_header EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  auth_header_file=$(umask 077 && mktemp "${TMPDIR:-/tmp}/marimo-pair-auth.XXXXXX")
  printf 'Authorization: Bearer %s\n' "$token" > "$auth_header_file"
  auth_args+=(-H "@${auth_header_file}")
fi
```

Both existing `curl` calls already expand `${auth_args[@]...}`; leave them unchanged.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -B -m unittest tests.test_execute_code_transport -v`
Expected: all 12 tests PASS.

- [ ] **Step 6: Run the branch unit suites (they shell out to execute-code.sh)**

Run: `python3 -B -m unittest tests.test_branch tests.test_branch_state -v`
Expected: only the 3 baseline failures.

- [ ] **Step 7: Commit**

```bash
git add skills/marimo-pair/scripts/execute-code.sh tests/test_execute_code_transport.py
git commit -m "feat: read execute-code tokens from files and keep them out of curl argv

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `branch.py` parent-token resolution, `--token-file`, and child env hygiene

**Files:**
- Modify: `skills/marimo-pair/scripts/branch.py` (new helpers after `redact_log_text` ~line 210; `_bash_environment` line 547-551; `WorkerManager.__init__` ~line 632; `parent_target` lines 733-747; `_validate_parent` lines 852-854; `_export_parent` lines 915-944; `start_worker` except block line 979; `_child_environment` lines 996-1000; `_read_log_tail` ~1041; `_run_required` ~1217; `_launch_and_initialize` ~1275; `_execute_record` lines 1417-1451; `_shutdown` ~1539; `main`)
- Test: `tests/test_branch.py`

**Interfaces:**
- Consumes: Task 1's `execute-code.sh` precedence (`MARIMO_TOKEN_FILE` outranks `MARIMO_TOKEN`).
- Produces:
  - `branch.parent_token(token_file: str | None = None) -> str` (raises `BranchError`)
  - `branch._without_token_sources(environ: Mapping[str, str]) -> dict[str, str]`
  - `WorkerManager(..., parent_token_file: str | None = None)`; attribute `parent_token_file`
  - `WorkerManager._parent_token() -> str`; `WorkerManager._redaction_tokens(*tokens: str) -> list[str]`
  - CLI: `start` and `add` accept `--token-file PATH`; `main` sets `manager.parent_token_file = args.token_file` (when present).
  - Test helper in `tests/test_branch.py`: `_record(directory: Path, **overrides) -> branch.WorkerRecord`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_branch.py` add `import io` to the imports, and add this module-level helper after the imports:

```python
def _record(directory: Path, **overrides) -> branch.WorkerRecord:
    directory = Path(directory)
    values = dict(
        worker_id="a" * 32, url="http://127.0.0.1:1234", session_id="worker-session",
        token_path=directory / "worker-token", notebook_path=directory / "worker.py",
        temp_dir=directory, runtime_path=directory / "runtime.json",
        parent_url="http://127.0.0.1:2718", parent_notebook_path=directory / "parent.py",
        parent_session_id="s_parent", status="running", server_pid=None, process_identity=None,
        imported_names=(), log_path=directory / "worker.log",
        bundle_path=directory / "values.pickle", helper_path=directory / "branch_state.py",
    )
    values.update(overrides)
    return branch.WorkerRecord(**values)
```

Add a new test class:

```python
class ParentTokenTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)

    def _file(self, name, data: bytes) -> str:
        path = self.directory / name
        path.write_bytes(data)
        return str(path)

    def test_precedence_is_argument_then_env_file_then_env_token(self):
        flag = self._file("flag", b"flag-secret\n")
        env_file = self._file("env", b"env-file-secret\r\n")
        with mock.patch.dict(os.environ, {"MARIMO_TOKEN_FILE": env_file, "MARIMO_TOKEN": "env-secret"}):
            self.assertEqual(branch.parent_token(flag), "flag-secret")
            self.assertEqual(branch.parent_token(None), "env-file-secret")
        with mock.patch.dict(os.environ, {"MARIMO_TOKEN": "env-secret"}, clear=False):
            os.environ.pop("MARIMO_TOKEN_FILE", None)
            self.assertEqual(branch.parent_token(None), "env-secret")

    def test_invalid_sources_raise_branch_errors(self):
        cases = [
            (self._file("empty", b"\n"), "empty"),
            (self._file("multi", b"first\nsecond\n"), "control"),
            (str(self.directory / "missing"), "unreadable"),
        ]
        for path, message in cases:
            with self.subTest(message=message), self.assertRaises(BranchError) as caught:
                branch.parent_token(path)
            self.assertIn(message, str(caught.exception).lower())
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(BranchError) as caught:
            branch.parent_token(None)
        self.assertIn("MARIMO_TOKEN_FILE", str(caught.exception))

    def test_worker_execution_env_drops_parent_token_file(self):
        manager = WorkerManager(root_dir=self.directory / "home")
        record = _record(self.directory)
        record.token_path.write_text("worker-secret", encoding="utf-8")
        parent_file = self._file("parent-token", b"parent-file-secret\n")
        with (
            mock.patch.dict(os.environ, {"MARIMO_TOKEN_FILE": parent_file, "MARIMO_TOKEN": "parent-env"}),
            mock.patch.object(manager, "_validate_worker", return_value=None),
            mock.patch.object(manager, "_bash", return_value=Path("/bin/bash")),
            mock.patch.object(branch.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout="leaked parent-file-secret", stderr="")) as run,
        ):
            result = manager.execute(record.worker_id, "print(1)", record=record)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["MARIMO_TOKEN"], "worker-secret")
        self.assertNotIn("MARIMO_TOKEN_FILE", env)
        self.assertNotIn("parent-file-secret", result.output)

    def test_parent_export_uses_file_token_and_redacts_it(self):
        manager = WorkerManager(root_dir=self.directory / "home")
        manager.parent_token_file = self._file("parent-token", b"parent-file-secret\n")
        record = _record(self.directory)
        parent = ParentTarget("http://127.0.0.1:2718", "s_parent", self.directory / "parent.py")
        with (
            mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-env"}),
            mock.patch.object(manager, "_bash", return_value=Path("/bin/bash")),
            mock.patch.object(branch.subprocess, "run", return_value=mock.Mock(
                returncode=1, stdout="", stderr="echo parent-file-secret")) as run,
        ):
            with self.assertRaises(BranchError) as caught:
                manager._export_parent(record, parent, ["records"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["MARIMO_TOKEN"], "parent-file-secret")
        self.assertNotIn("MARIMO_TOKEN_FILE", env)
        self.assertNotIn("parent-file-secret", str(caught.exception))

    def test_worker_server_environment_has_no_token_sources(self):
        manager = WorkerManager(root_dir=self.directory / "home")
        with mock.patch.dict(os.environ, {"MARIMO_TOKEN_FILE": "/x", "MARIMO_TOKEN": "y"}):
            env = manager._child_environment()
            bash_env = branch._bash_environment()
        for candidate in (env, bash_env):
            self.assertNotIn("MARIMO_TOKEN", candidate)
            self.assertNotIn("MARIMO_TOKEN_FILE", candidate)

    def test_cli_start_reads_token_file_argument(self):
        missing = str(self.directory / "missing-token")
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"MARIMO_PAIR_HOME": str(self.directory / "home"), "MARIMO_TOKEN": "env"}), \
                mock.patch.object(branch.sys, "stderr", stderr):
            result = branch.main([
                "start", "--parent-url", "http://127.0.0.1:2718", "--parent-session", "s",
                "--token-file", missing, "--names", "records",
            ])
        self.assertEqual(result, 1)
        self.assertIn("missing-token", stderr.getvalue())
```

In the existing `test_execute_script_uses_explicit_url_session_and_utf8_stdin_never_dash_c`, change the env patch to `mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret", "MARIMO_TOKEN_FILE": "/parent/token"})` and add after the existing `MARIMO_TOKEN` assertions:

```python
            self.assertNotIn("MARIMO_TOKEN_FILE", run.call_args.kwargs["env"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -B -m unittest tests.test_branch.ParentTokenTests -v`
Expected: FAIL / ERROR (`module 'branch' has no attribute 'parent_token'`, `unrecognized arguments: --token-file`).

- [ ] **Step 3: Implement**

Add `from typing import Any, Iterable, Iterator, Mapping, Sequence` (add `Mapping`). After `redact_log_text`, add:

```python
def _checked_token(value: str, source: str) -> str:
    if not value:
        raise BranchError(f"Parent token from {source} is empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BranchError(f"Parent token from {source} contains control characters")
    return value


def parent_token(token_file: str | None = None) -> str:
    """Resolve the parent bearer token: --token-file, MARIMO_TOKEN_FILE, then MARIMO_TOKEN."""
    path = token_file or os.environ.get("MARIMO_TOKEN_FILE")
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise BranchError(f"Token file is unreadable: {path}: {error}") from error
        return _checked_token(value.rstrip("\r\n"), f"token file {path}")
    value = os.environ.get("MARIMO_TOKEN")
    if not value:
        raise BranchError(
            "Set MARIMO_TOKEN_FILE (or pass --token-file) to authenticate to the parent session"
        )
    return _checked_token(value, "MARIMO_TOKEN")


def _without_token_sources(environ: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment without either token source; callers set MARIMO_TOKEN explicitly."""
    env = dict(environ)
    env.pop("MARIMO_TOKEN", None)
    env.pop("MARIMO_TOKEN_FILE", None)
    return env
```

In `_bash_environment`, replace the copy/pop of `MARIMO_TOKEN` with `env = _without_token_sources(os.environ)` (keep the rest of the function).

`WorkerManager.__init__`: add keyword `parent_token_file: str | None = None` and `self.parent_token_file = parent_token_file`. Add methods next to `read_token`:

```python
    def _parent_token(self) -> str:
        return parent_token(self.parent_token_file)

    def _redaction_tokens(self, *tokens: str) -> list[str]:
        """Secrets to scrub from errors: the given tokens plus the parent token, if resolvable."""
        try:
            known = self._parent_token()
        except BranchError:
            known = ""
        return [*tokens, known]
```

In `parent_target` and `_validate_parent`, replace

```python
        token = os.environ.get("MARIMO_TOKEN")
        if not token:
            raise BranchError("Set MARIMO_TOKEN to authenticate to the parent session")
```

with `token = self._parent_token()`.

In `_export_parent`, replace

```python
        env = os.environ.copy()
        parent_token = env.pop("MARIMO_TOKEN", None)
        if not parent_token:
            raise BranchError("Set MARIMO_TOKEN to authenticate to the parent session")
        env["MARIMO_TOKEN"] = parent_token
```

with

```python
        token = self._parent_token()
        env = _without_token_sources(os.environ)
        env["MARIMO_TOKEN"] = token
```

and change the redaction at the end of `_export_parent` from `[parent_token]` to `[token]`.

In `_child_environment`, replace `env = os.environ.copy()` / `env.pop("MARIMO_TOKEN", None)` with `env = _without_token_sources(os.environ)`.

In `_execute_record`, replace `env = os.environ.copy()` / `env.pop("MARIMO_TOKEN", None)` with `env = _without_token_sources(os.environ)` (keep `env["MARIMO_TOKEN"] = token`).

Replace every remaining `[token, os.environ.get("MARIMO_TOKEN", "")]` in `branch.py` with `self._redaction_tokens(token)` (occurrences in `start_worker`, `_read_log_tail`, `_run_required`, `_launch_and_initialize`, `_execute_record` ×4, `_shutdown`), and `os.environ.get("MARIMO_TOKEN", "")` inside `start_worker`'s redaction likewise. Verify with `grep -n 'environ.get("MARIMO_TOKEN"' skills/marimo-pair/scripts/branch.py` → no output.

In `main`, add to both `start` and `add` parsers:

```python
    start.add_argument(
        "--token-file",
        help="file holding the parent token (default: $MARIMO_TOKEN_FILE, then $MARIMO_TOKEN)",
    )
```

(same for `add`), and right after `manager = WorkerManager()`:

```python
    manager.parent_token_file = getattr(args, "token_file", None)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -B -m unittest tests.test_branch tests.test_branch_state tests.test_execute_code_transport -v`
Expected: only the 3 baseline failures.

- [ ] **Step 5: Commit**

```bash
git add skills/marimo-pair/scripts/branch.py tests/test_branch.py
git commit -m "feat: resolve the branch parent token from a file and isolate child token env

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Resolve the parent session from key, `session_id`, notebook, or lone session

**Files:**
- Modify: `skills/marimo-pair/scripts/branch.py` (replace `parent_target` lines 733-747 with `resolve_parent`; `add_worker` lines 1336-1361; `main` start/add parsers and handlers)
- Test: `tests/test_branch.py`

**Interfaces:**
- Consumes: `WorkerManager._parent_token()` (Task 2), existing `_validate_parent_url`, `_sessions`, `_session_path`, `_same_notebook_path`, `_real_path`.
- Produces:
  - `WorkerManager.resolve_parent(url: str, *, notebook: str | Path | None = None, session: str | None = None) -> ParentTarget` — `session_id` of the result is always the listing key; `notebook_path` is the real path.
  - `WorkerManager.add_worker(worker_id: str, parent_session_id: str | None, names: Sequence[str]) -> None`
  - CLI: `start --parent-url URL [--parent-notebook PATH] [--parent-session KEY_OR_ID]`; `add --worker ID [--parent-session KEY_OR_ID]`.
  - `parent_target` is removed.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_branch.py`:

```python
class ParentResolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)
        self.manager = WorkerManager(root_dir=self.directory / "home")
        self.a = self.directory / "a.py"
        self.b = self.directory / "b.py"
        self.a.write_text("", encoding="utf-8")
        self.b.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _resolve(self, sessions, status=200, **kwargs):
        with mock.patch.object(self.manager, "_sessions", return_value=(status, sessions)):
            return self.manager.resolve_parent("http://localhost:2718/", **kwargs)

    def test_session_key_and_session_id_field_both_resolve_to_the_key(self):
        sessions = {"s_a": {"session_id": "sess-1", "path": str(self.a)}}
        for session in ("s_a", "sess-1"):
            with self.subTest(session=session):
                target = self._resolve(sessions, session=session)
                self.assertEqual(target, ParentTarget("http://localhost:2718", "s_a", self.a))

    def test_notebook_selects_its_session_including_relative_and_symlinked_paths(self):
        sessions = {"s_a": {"path": str(self.a)}, "s_b": {"path": str(self.b)}}
        link = self.directory / "link.py"
        link.symlink_to(self.a)
        cwd = os.getcwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, cwd)
        for notebook in (self.a, "a.py", link):
            with self.subTest(notebook=str(notebook)):
                self.assertEqual(self._resolve(sessions, notebook=notebook).session_id, "s_a")

    def test_ambiguous_and_missing_notebooks_fail_with_session_keys(self):
        with self.assertRaises(BranchError) as caught:
            self._resolve({"s_1": {"path": str(self.a)}, "s_2": {"path": str(self.a)}}, notebook=self.a)
        self.assertIn("s_1", str(caught.exception))
        self.assertIn("s_2", str(caught.exception))
        self.assertIn("--parent-session", str(caught.exception))
        with self.assertRaises(BranchError) as caught:
            self._resolve({"s_a": {"path": str(self.a)}}, notebook=self.b)
        self.assertIn("No active parent session", str(caught.exception))
        self.assertIn("s_a", str(caught.exception))

    def test_lone_session_is_the_default_and_several_require_a_selector(self):
        self.assertEqual(self._resolve({"s_a": {"path": str(self.a)}}).session_id, "s_a")
        for sessions in ({}, {"s_a": {"path": str(self.a)}, "s_b": {"path": str(self.b)}}):
            with self.subTest(count=len(sessions)), self.assertRaises(BranchError) as caught:
                self._resolve(sessions)
            self.assertIn("--parent-notebook", str(caught.exception))

    def test_lone_session_without_a_notebook_path_is_rejected_clearly(self):
        with self.assertRaises(BranchError) as caught:
            self._resolve({"s_new": {"path": None}})
        self.assertIn("no notebook path", str(caught.exception))

    def test_session_and_notebook_must_agree(self):
        with self.assertRaises(BranchError):
            self._resolve({"s_a": {"path": str(self.a)}}, session="s_a", notebook=self.b)

    def test_unauthenticated_listing_fails(self):
        with self.assertRaises(BranchError) as caught:
            self._resolve(None, status=401, session="s_a")
        self.assertIn("authenticate", str(caught.exception))

    def test_add_resolves_the_current_session_from_the_worker_notebook(self):
        record = mock.Mock(
            worker_id="b" * 32, imported_names=("records",), status="running",
            parent_url="http://127.0.0.1:1234", parent_notebook_path=self.a,
        )
        resolved = ParentTarget("http://127.0.0.1:1234", "s_after_reconnect", self.a)
        with mock.patch.object(self.manager, "_read_record", return_value=record), mock.patch.object(
            self.manager, "_validate_worker"
        ), mock.patch.object(self.manager, "_inspect_worker_names", return_value=set()), mock.patch.object(
            self.manager, "resolve_parent", return_value=resolved
        ) as resolve, mock.patch.object(
            self.manager, "_validate_parent", side_effect=BranchError("stop")
        ) as validate, mock.patch.object(self.manager, "_export_parent") as export:
            with self.assertRaises(BranchError):
                self.manager.add_worker(record.worker_id, None, ["new_value"])
        resolve.assert_called_once_with(record.parent_url, notebook=self.a, session=None)
        self.assertEqual(validate.call_args.args[0].session_id, "s_after_reconnect")
        export.assert_not_called()

    def test_cli_start_passes_notebook_and_optional_session(self):
        with mock.patch.dict(os.environ, {"MARIMO_PAIR_HOME": str(self.directory / "home")}), mock.patch.object(
            branch.WorkerManager, "resolve_parent", side_effect=BranchError("stop")
        ) as resolve, mock.patch.object(branch.sys, "stderr", io.StringIO()):
            self.assertEqual(branch.main([
                "start", "--parent-url", "http://localhost:2718", "--parent-notebook", str(self.a),
                "--names", "records",
            ]), 1)
        resolve.assert_called_once_with("http://localhost:2718", notebook=str(self.a), session=None)
```

Update existing tests that referenced removed or changed APIs:

- `test_start_cli_rejects_private_names_before_parent_contact`: patch `"resolve_parent"` instead of `"parent_target"` (rename the variable to `resolve_parent`).
- `test_cli_start_refuses_process_local_sse_drainer`: patch `"resolve_parent"` instead of `"parent_target"` (same `return_value`).
- `test_add_rejects_all_private_names_and_existing_worker_names_before_parent_contact`: add `mock.patch.object(manager, "resolve_parent") as resolve_parent` to the `with` and `resolve_parent.assert_not_called()` after it.
- `test_add_validates_fresh_parent_session_path_before_export` and `test_failed_add_rolls_back_new_cell_and_artifacts`: add to each `with`:

```python
mock.patch.object(manager, "resolve_parent", return_value=ParentTarget(
    "http://127.0.0.1:1234", "fresh-session", Path("parent.py"))),
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -B -m unittest tests.test_branch.ParentResolutionTests -v`
Expected: ERROR `'WorkerManager' object has no attribute 'resolve_parent'`.

- [ ] **Step 3: Implement `resolve_parent`**

Replace the whole `parent_target` method with:

```python
    def resolve_parent(
        self, url: str, *, notebook: str | Path | None = None, session: str | None = None,
    ) -> ParentTarget:
        """Select the parent session by key, session_id field, notebook path, or lone session."""
        base = self._validate_parent_url(url)
        status, sessions = self._sessions(base, self._parent_token())
        if not 200 <= status < 300 or sessions is None:
            raise BranchError("Could not authenticate to the parent session list")
        entries = [(key, _session_path(entry), entry) for key, entry in sessions.items()]

        def listing() -> str:
            return "; ".join(f"{key} -> {path or '<no path>'}" for key, path, _ in entries) or "none"

        if session is not None:
            matches = [item for item in entries if item[0] == session] or [
                item for item in entries
                if isinstance(item[2], dict) and item[2].get("session_id") == session
            ]
            if not matches:
                raise BranchError(f"Parent session {session!r} is not active; open sessions: {listing()}")
            key, path, _ = matches[0]
            if notebook is not None and (not path or not _same_notebook_path(path, notebook)):
                raise BranchError(f"Parent session {key!r} does not map to notebook {notebook}")
        elif notebook is not None:
            matches = [item for item in entries if item[1] and _same_notebook_path(item[1], notebook)]
            if not matches:
                raise BranchError(f"No active parent session for {notebook}; open sessions: {listing()}")
            if len(matches) > 1:
                keys = ", ".join(key for key, _, _ in matches)
                raise BranchError(
                    f"{len(matches)} sessions are open for {notebook}: {keys}; pass --parent-session"
                )
            key, path, _ = matches[0]
        else:
            if len(entries) != 1:
                raise BranchError(
                    f"{len(entries)} parent sessions are open; pass --parent-notebook or "
                    f"--parent-session. Open sessions: {listing()}"
                )
            key, path, _ = entries[0]
        if not path:
            raise BranchError(f"Parent session {key!r} has no notebook path")
        real = _real_path(path)
        if not real.is_file():
            raise BranchError(f"Parent notebook is not a regular file: {real}")
        return ParentTarget(base, key, real)
```

- [ ] **Step 4: Make `add_worker` resolve the current session**

Change the signature to `def add_worker(self, worker_id: str, parent_session_id: str | None, names: Sequence[str]) -> None:` and replace

```python
        parent = ParentTarget(record.parent_url, parent_session_id, record.parent_notebook_path)
        canonical_parent = self._validate_parent(parent)
        if not _same_notebook_path(canonical_parent, record.parent_notebook_path):
            raise BranchError("Fresh parent session does not map to this worker's original notebook")
        record.parent_session_id = parent_session_id
```

with

```python
        # Resolve the *current* session for this worker's notebook; browser
        # reconnects rename sessions, so a stored key may be stale.
        resolved = self.resolve_parent(
            record.parent_url, notebook=record.parent_notebook_path, session=parent_session_id,
        )
        parent = ParentTarget(record.parent_url, resolved.session_id, record.parent_notebook_path)
        canonical_parent = self._validate_parent(parent)
        if not _same_notebook_path(canonical_parent, record.parent_notebook_path):
            raise BranchError("Fresh parent session does not map to this worker's original notebook")
        record.parent_session_id = parent.session_id
```

- [ ] **Step 5: Update the CLI**

Replace the `start` and `add` parser definitions (keep `--token-file` from Task 2) with:

```python
    start = subparsers.add_parser("start", help="start a worker from named parent values")
    start.add_argument(
        "--parent-url", required=True,
        help="loopback URL of the token-protected parent server, e.g. http://localhost:2718",
    )
    start.add_argument("--parent-notebook", help="parent notebook path; selects that notebook's session")
    start.add_argument(
        "--parent-session",
        help="exact parent session key or session_id (default: from --parent-notebook or the lone session)",
    )
    start.add_argument("--names", nargs="+", required=True, help="parent variable names to copy")
    start.add_argument(
        "--token-file",
        help="file holding the parent token (default: $MARIMO_TOKEN_FILE, then $MARIMO_TOKEN)",
    )

    add = subparsers.add_parser("add", help="coordinator-only named export to a worker")
    add.add_argument("--worker", required=True, help="worker ID printed by start")
    add.add_argument(
        "--parent-session",
        help="exact parent session key or session_id (default: current session of the worker's notebook)",
    )
    add.add_argument("--names", nargs="+", required=True, help="additional parent variable names")
    add.add_argument(
        "--token-file",
        help="file holding the parent token (default: $MARIMO_TOKEN_FILE, then $MARIMO_TOKEN)",
    )
```

Also give `run`/`stop` workers a help string: `run.add_argument("--worker", required=True, help="worker ID; code is read from stdin")`, `run.add_argument("source", choices=["-"], help="'-' to read code from stdin")`, `stop.add_argument("--worker", required=True, help="worker ID to stop")`.

In the `start` handler replace `parent = manager.parent_target(args.parent_url, args.parent_session)` with:

```python
            parent = manager.resolve_parent(
                args.parent_url, notebook=args.parent_notebook, session=args.parent_session,
            )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -B -m unittest tests.test_branch tests.test_branch_state tests.test_execute_code_transport -v`
Expected: only the 3 baseline failures.

- [ ] **Step 7: Commit**

```bash
git add skills/marimo-pair/scripts/branch.py tests/test_branch.py
git commit -m "feat: resolve branch parent sessions from notebook path or lone session

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `start_workers` — one export, N workers, all-or-nothing; `start --count`

**Files:**
- Modify: `skills/marimo-pair/scripts/branch.py` (module constant near the top; replace `start_worker` lines 946-984 with `start_worker` + `start_workers` + `_worker_tokens`; `main` start parser/handler; new module function `_worker_count`; add `import argparse` at the top and drop the local import in `main`)
- Test: `tests/test_branch.py`

**Interfaces:**
- Consumes: `resolve_parent` (Task 3), `_redaction_tokens` (Task 2), existing `_validate_parent`, `_new_record`, `_export_parent`, `_launch_and_initialize`, `_abort_worker`, `_write_record`.
- Produces:
  - `branch.MAX_WORKERS_PER_START = 16`
  - `WorkerManager.start_workers(parent: ParentTarget, names: Sequence[str], count: int) -> list[WorkerRecord]`
  - `WorkerManager.start_worker(parent, names) -> WorkerRecord` (now `start_workers(parent, names, 1)[0]`)
  - CLI `start --count N`; output object/list each including `parent_session`.

- [ ] **Step 1: Write the failing tests**

Add `import contextlib` and `import shutil` to `tests/test_branch.py` imports, and add:

```python
def _fake_runtime(notebook: Path, directory: Path) -> dict:
    return {
        "notebook_path": str(Path(notebook).resolve()), "config_sources": {}, "cwd": str(directory),
        "sys_path": [], "python_executable": sys.executable,
        "python_version": platform.python_version(), "pythonpath": [], "dotenv": [],
        "sandbox_context": {}, "dependency_context": {
            "implementation": platform.python_implementation(), "python_prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
        },
    }


class StartWorkersTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.manager = WorkerManager(root_dir=self.directory / "home", startup_timeout=0.2, request_timeout=0.2)
        self.notebook = self.directory / "parent.py"
        self.notebook.write_text("", encoding="utf-8")
        self.parent = ParentTarget("http://127.0.0.1:1234", "s_parent", self.notebook)
        self.exports = []
        self.launches = []

    def _export(self, record, parent, names):
        self.exports.append(record.worker_id)
        record.runtime_path.write_text(json.dumps(_fake_runtime(self.notebook, self.directory)), encoding="utf-8")
        record.bundle_path.write_bytes(b"values")

    def _launch(self, record, runtime):
        self.launches.append({
            "worker_id": record.worker_id, "bundle": record.bundle_path,
            "bundle_exists": record.bundle_path.exists(), "helper_exists": record.helper_path.exists(),
            "notebook_exists": record.notebook_path.exists(),
        })

    @contextlib.contextmanager
    def _patched(self, launch):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}))
            stack.enter_context(mock.patch.object(
                self.manager, "_validate_parent", return_value=str(self.notebook.resolve())))
            stack.enter_context(mock.patch.object(self.manager, "_export_parent", side_effect=self._export))
            stack.enter_context(mock.patch.object(self.manager, "_launch_and_initialize", side_effect=launch))
            yield

    def test_one_export_restores_every_worker_then_shared_files_are_removed(self):
        with self._patched(self._launch):
            records = self.manager.start_workers(self.parent, ["records"], 3)
        self.assertEqual(len(self.exports), 1)
        self.assertEqual(len({record.worker_id for record in records}), 3)
        self.assertEqual(len({launch["bundle"] for launch in self.launches}), 1)
        self.assertTrue(all(
            launch["bundle_exists"] and launch["helper_exists"] and launch["notebook_exists"]
            for launch in self.launches
        ))
        self.assertFalse(self.launches[0]["bundle"].exists())
        for record in records:
            self.assertEqual(record.status, "running")
            # Later `add` batches must use the worker's own private paths.
            self.assertEqual(record.bundle_path.parent, record.temp_dir)
            self.assertEqual(record.runtime_path.parent, record.temp_dir)
            self.assertFalse(record.helper_path.exists())
            stored = self.manager.get_worker(record.worker_id)
            self.assertEqual(stored.bundle_path, record.bundle_path)
            self.assertEqual(stored.status, "running")

    def test_failure_on_a_later_worker_stops_every_earlier_worker(self):
        def launch(record, runtime):
            self._launch(record, runtime)
            if len(self.launches) == 3:
                raise BranchError("bootstrap failed")

        with self._patched(launch), self.assertRaises(BranchError) as caught:
            self.manager.start_workers(self.parent, ["records"], 3)
        self.assertIn("at worker 3", str(caught.exception))
        self.assertIn("bootstrap failed", str(caught.exception))
        self.assertEqual(list(self.manager.registry_dir.glob("*.json")), [])
        self.assertEqual(list(self.manager.temp_root.iterdir()), [])

    def test_incomplete_cleanup_lists_every_remaining_worker(self):
        def launch(record, runtime):
            self._launch(record, runtime)
            if len(self.launches) == 2:
                raise BranchError("bootstrap failed")

        with self._patched(launch), mock.patch.object(self.manager, "_abort_worker", return_value=False), \
                self.assertRaises(BranchError) as caught:
            self.manager.start_workers(self.parent, ["records"], 3)
        self.assertIn("remain registered", str(caught.exception))
        for launch_info in self.launches:
            self.assertIn(launch_info["worker_id"], str(caught.exception))

    def test_interrupt_during_a_later_launch_cleans_everything(self):
        def launch(record, runtime):
            self._launch(record, runtime)
            if len(self.launches) == 2:
                raise KeyboardInterrupt()

        with self._patched(launch), self.assertRaises(KeyboardInterrupt):
            self.manager.start_workers(self.parent, ["records"], 2)
        self.assertEqual(list(self.manager.registry_dir.glob("*.json")), [])
        self.assertEqual(list(self.manager.temp_root.iterdir()), [])

    def test_count_bounds_are_checked_before_parent_contact(self):
        with mock.patch.object(self.manager, "_validate_parent") as validate:
            for count in (0, branch.MAX_WORKERS_PER_START + 1):
                with self.subTest(count=count), self.assertRaises(BranchError):
                    self.manager.start_workers(self.parent, ["records"], count)
        validate.assert_not_called()

    def test_cli_prints_an_object_without_count_and_a_list_with_count(self):
        records = [
            mock.Mock(worker_id=str(i) * 32, url=f"http://127.0.0.1:{4000 + i}", session_id=f"w{i}", status="running")
            for i in range(2)
        ]
        parent = ParentTarget("http://127.0.0.1:2718", "s_parent", Path("parent.py"))

        def initialize(manager, *args, **kwargs):
            manager._drainers = {}

        for argv, count in (([], 1), (["--count", "2"], 2)):
            stdout = io.StringIO()
            with self.subTest(argv=argv), mock.patch.object(
                branch.WorkerManager, "__init__", autospec=True, side_effect=initialize
            ), mock.patch.object(branch.WorkerManager, "resolve_parent", return_value=parent), mock.patch.object(
                branch.WorkerManager, "start_workers", side_effect=lambda p, n, c: records[:c]
            ) as start, mock.patch.object(branch.sys, "stdout", stdout):
                self.assertEqual(branch.main([
                    "start", "--parent-url", "http://127.0.0.1:2718", "--names", "records", *argv,
                ]), 0)
                start.assert_called_once_with(parent, ["records"], count)
                printed = json.loads(stdout.getvalue())
                if argv:
                    self.assertEqual([item["worker_id"] for item in printed], [r.worker_id for r in records])
                    self.assertTrue(all(item["parent_session"] == "s_parent" for item in printed))
                else:
                    self.assertEqual(printed["worker_id"], records[0].worker_id)
                    self.assertEqual(printed["parent_session"], "s_parent")

    def test_cli_rejects_out_of_range_counts(self):
        for value in ("0", "17", "two"):
            with self.subTest(value=value), mock.patch.object(branch.sys, "stderr", io.StringIO()), \
                    self.assertRaises(SystemExit):
                branch.main([
                    "start", "--parent-url", "http://127.0.0.1:2718", "--names", "records", "--count", value,
                ])
```

Update `test_cli_start_refuses_process_local_sse_drainer`: change `mock.patch.object(branch.WorkerManager, "start_worker", return_value=record)` to `mock.patch.object(branch.WorkerManager, "start_workers", return_value=[record])`, and change `def initialize(manager):` to `def initialize(manager, *args, **kwargs):`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -B -m unittest tests.test_branch.StartWorkersTests -v`
Expected: ERROR `'WorkerManager' object has no attribute 'start_workers'` / `unrecognized arguments: --count`.

- [ ] **Step 3: Implement `start_workers`**

Add `import argparse` to the top-level imports and remove the `import argparse` line inside `main`. Below the `BranchError` class add:

```python
# Upper bound for one `start --count`; guards against typos spawning many servers.
MAX_WORKERS_PER_START = 16
```

Replace the whole `start_worker` method with:

```python
    def start_worker(self, parent: ParentTarget, names: Sequence[str]) -> WorkerRecord:
        """Snapshot named parent values, then launch and validate an isolated server."""
        return self.start_workers(parent, names, 1)[0]

    def start_workers(self, parent: ParentTarget, names: Sequence[str], count: int) -> list[WorkerRecord]:
        """Export named parent values once, then launch ``count`` workers from that snapshot.

        Startup is all-or-nothing: if any step fails, every worker created so far
        is stopped and its files removed before the error propagates.
        """
        if not 1 <= count <= MAX_WORKERS_PER_START:
            raise BranchError(f"Worker count must be between 1 and {MAX_WORKERS_PER_START}")
        selected = validate_names(names)
        # Parent identity is checked before any worker directory/registry is created.
        parent_notebook = self._validate_parent(parent)
        records: list[WorkerRecord] = []
        try:
            first = self._new_record(parent, parent_notebook, selected)
            records.append(first)
            self._write_record(first)
            # One export serves every worker. It lives in the first worker's private
            # directory, so an interrupted start is recovered by list/stop.
            self._export_parent(first, parent, selected)
            runtime = read_parent_runtime(first.runtime_path)
            if not _same_notebook_path(runtime.notebook_path, first.parent_notebook_path):
                raise BranchError("Parent runtime metadata identifies a different notebook")
            notebook_text = build_worker_notebook(runtime)
            for index in range(count):
                if index == 0:
                    record = first
                else:
                    record = self._new_record(parent, parent_notebook, selected)
                    record.bundle_path = first.bundle_path
                    record.runtime_path = first.runtime_path
                    records.append(record)
                    self._write_record(record)
                shutil.copyfile(Path(branch_state.__file__).resolve(), record.helper_path)
                if os.name != "nt":
                    record.helper_path.chmod(0o600)
                _write_private(record.notebook_path, notebook_text)
                self._launch_and_initialize(record, runtime)
            # The values and metadata are no longer needed after every bootstrap has run.
            self._remove_path(first.bundle_path)
            self._remove_path(first.runtime_path)
            for record in records:
                self._remove_path(record.helper_path)
                # Later `add` batches use each worker's own private paths.
                record.bundle_path = record.temp_dir / "values.pickle"
                record.runtime_path = record.temp_dir / "runtime.json"
                record.status = "running"
                self._write_record(record)
            return records
        except BaseException as error:
            # Read worker tokens for redaction before cleanup deletes them.
            secrets_to_redact = self._redaction_tokens(*self._worker_tokens(records))
            remaining = [record.worker_id for record in reversed(records) if not self._abort_worker(record)]
            if not isinstance(error, Exception):
                raise
            detail = redact_log_text(str(error), secrets_to_redact)
            if remaining:
                raise BranchError(
                    f"Worker startup failed and cleanup is incomplete; workers {', '.join(remaining)} "
                    f"remain registered for recovery: {detail}"
                ) from error
            if count > 1:
                raise BranchError(
                    f"Starting {count} workers failed at worker {len(records)}; "
                    f"every started worker was stopped: {detail}"
                ) from error
            raise

    def _worker_tokens(self, records: Sequence[WorkerRecord]) -> list[str]:
        tokens = []
        for record in records:
            try:
                tokens.append(self.read_token(record))
            except BranchError:
                continue
        return tokens
```

- [ ] **Step 4: Implement the CLI**

Add a module function above `main`:

```python
def _worker_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"--count must be an integer, got {value!r}") from error
    if not 1 <= count <= MAX_WORKERS_PER_START:
        raise argparse.ArgumentTypeError(f"--count must be between 1 and {MAX_WORKERS_PER_START}")
    return count
```

Add to the `start` parser:

```python
    start.add_argument(
        "--count", type=_worker_count,
        help=f"start N workers from one export of the parent (1-{MAX_WORKERS_PER_START}); prints a JSON list",
    )
```

Replace the body of `if args.command == "start":` with:

```python
        if args.command == "start":
            selected = validate_names(args.names)
            parent = manager.resolve_parent(
                args.parent_url, notebook=args.parent_notebook, session=args.parent_session,
            )
            records = manager.start_workers(parent, selected, args.count or 1)
            if any(record.worker_id in manager._drainers for record in records):
                cleanup_errors = []
                for record in reversed(records):
                    try:
                        manager.stop_worker(record.worker_id)
                    except BranchError as cleanup_error:
                        cleanup_errors.append(f"{record.worker_id}: {cleanup_error}")
                if cleanup_errors:
                    raise BranchError(
                        "Worker startup requires an SSE drainer that cannot survive this CLI process; "
                        "cleanup also failed: " + "; ".join(cleanup_errors)
                    )
                raise BranchError(
                    "Worker startup requires a process-local SSE drainer; use a persistent coordinator "
                    "instead of the short-lived start CLI"
                )
            started = [
                {
                    "worker_id": record.worker_id,
                    "url": record.url,
                    "session_id": record.session_id,
                    "status": record.status,
                    "parent_session": parent.session_id,
                }
                for record in records
            ]
            print(json.dumps(started if args.count is not None else started[0]))
            return 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -B -m unittest tests.test_branch tests.test_branch_state tests.test_execute_code_transport -v`
Expected: only the 3 baseline failures (the existing single-worker start/cancellation/failure tests still pass via `start_worker`).

- [ ] **Step 6: Commit**

```bash
git add skills/marimo-pair/scripts/branch.py tests/test_branch.py
git commit -m "feat: start several branch workers from one parent export

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Real-server integration test for `--count`, `--parent-notebook`, `--token-file`

**Files:**
- Test: `tests/test_branch_integration.py` (new method in `WorkerIntegrationTests`, after `test_cli_start_run_add_and_stop_are_separate_processes`)

**Interfaces:**
- Consumes: CLI from Tasks 2–4; fixture helpers `_start_parent(root, "notebook")` → `(parent, notebook, ...)`, `ParentServer.url/.token/.session`, `_create_parent_cell(parent, source)`.
- Produces: nothing.

- [ ] **Step 1: Write the test**

```python
    def test_cli_count_notebook_selector_and_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, notebook, _, _, _ = self._start_parent(root, "notebook")
            manager_root = root / "cli-manager"
            token_file = root / "parent-token"
            token_file.write_text(parent.token + "\n", encoding="utf-8")
            if os.name != "nt":
                token_file.chmod(0o600)
            env = os.environ.copy()
            env.pop("MARIMO_TOKEN", None)
            env.pop("MARIMO_TOKEN_FILE", None)
            env.update({"MARIMO_PAIR_HOME": str(manager_root), "PYTHONDONTWRITEBYTECODE": "1"})
            script = SCRIPTS_DIR / "branch.py"

            def cli(*args, input_text=None):
                return subprocess.run(
                    [sys.executable, "-B", str(script), *args], input=input_text,
                    text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=180, env=env,
                )

            try:
                started = cli(
                    "start", "--parent-url", parent.url, "--parent-notebook", str(notebook),
                    "--token-file", str(token_file), "--count", "2", "--names", "records",
                )
                self.assertEqual(started.returncode, 0, started.stderr[-2000:])
                workers = json.loads(started.stdout)
                self.assertEqual(len(workers), 2)
                self.assertEqual({worker["parent_session"] for worker in workers}, {parent.session})
                for worker in workers:
                    ran = cli("run", "--worker", worker["worker_id"], "-",
                              input_text="records.append(2)\nprint(records)")
                    self.assertEqual(ran.returncode, 0, ran.stderr[-2000:])
                    # Each worker mutates its own copy of the one shared snapshot.
                    self.assertIn("[1, 2]", ran.stdout)

                # The second worker's later batch must use its own private paths.
                self._create_parent_cell(parent, "count_later = 'fresh'")
                added = cli("add", "--worker", workers[1]["worker_id"], "--token-file", str(token_file),
                            "--names", "count_later")
                self.assertEqual(added.returncode, 0, added.stderr[-2000:])
                ran_later = cli("run", "--worker", workers[1]["worker_id"], "-", input_text="print(count_later)")
                self.assertIn("fresh", ran_later.stdout)

                for worker in workers:
                    stopped = cli("stop", "--worker", worker["worker_id"])
                    self.assertEqual(stopped.returncode, 0, stopped.stderr[-2000:])
                self.assertEqual(WorkerManager(root_dir=manager_root).list_workers(), [])
            finally:
                WorkerManager(root_dir=manager_root).stop_all()
                parent.close()
```

- [ ] **Step 2: Run the integration suite**

Run: `MARIMO_PAIR_RUN_INTEGRATION=1 tests/notebooks/.venv/bin/python -B -m unittest tests.test_branch_integration -v`
Expected: all integration tests PASS, including the new one. (If the existing integration tests call `manager.start_worker(...)` directly they keep working; `start_worker` still exists.) If `tests/notebooks/.venv` lacks marimo, report that instead of claiming a pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_branch_integration.py
git commit -m "test: integration coverage for start --count with notebook selector and token file

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation

**Files:**
- Modify: `skills/marimo-pair/SKILL.md` (sections "Required first kernel command" and "Branch-and-Explore Workers")
- Modify: `skills/marimo-pair/reference/execution-context.md` ("Auth" and "Branch-and-Explore Workers")
- Modify: `skills/marimo-pair/reference/finding-marimo.md` (first paragraph)

**Interfaces:**
- Consumes: behaviour from Tasks 1–4.
- Produces: nothing.

- [ ] **Step 1: `execution-context.md` — Auth**

Replace the whole "## Auth" section body (from "For token-authenticated servers" through "execution requests.") with:

````markdown
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
````

- [ ] **Step 2: `execution-context.md` — Branch-and-Explore Workers**

Replace the paragraph starting "This optional coordinator workflow" through "never guess or reuse a stale ID." and the following `start` example with:

````markdown
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

`--count N` (1–16) exports the named values once and starts N workers from
that one snapshot, printing a JSON list (without `--count`, one JSON object).
Startup is all-or-nothing: if any worker fails to start, every worker from that
command is stopped. After `start` returns, workers are independent; a failing
`run` affects only its worker.
````

In the next paragraph, replace "using `execute-code.sh` with explicit `--url` and `--session`" with "using `execute-code.sh` with explicit `--url` and `--file /abs/path/notebook.py`", and replace the sentences from "Set `MARIMO_TOKEN` in the coordinator environment" through "Environment variables are inherited by children." with:

```markdown
Supply the parent token with `--token-file` (or `MARIMO_TOKEN_FILE`); do not
use an inline environment assignment, which may not match allowed command
patterns. Never pass the parent token or token file to branch agents.
```

In "Role boundary and commands", replace "the coordinator supplies the **current** parent session ID and runs:" and its `add` example with:

````markdown
the coordinator runs `add`, which finds the parent's **current** session for the
worker's notebook itself (browser reconnects rename sessions):

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py add \
  --worker WORKER_ID --token-file /path/to/token.txt --names new_value
```
````

- [ ] **Step 3: `finding-marimo.md`**

Replace

```markdown
If a server has a token, supply `MARIMO_TOKEN` in the coordinator's environment
rather than using `--token`. The script still passes the bearer header in
`curl`'s process arguments; this is not protection from other local processes.
```

with

```markdown
If a server has a token, supply it as a file (`--token-file` or
`MARIMO_TOKEN_FILE`) rather than `--token`. The script passes the header to
`curl` through a private temporary file, never in process arguments.
```

- [ ] **Step 4: `SKILL.md`**

In "Required first kernel command"/"Branch-and-Explore Workers":

1. Replace the paragraph beginning "Branching requires a token-protected parent; a `--no-token` server is rejected. The coordinator obtains its URL, token, and current session ID" through "Keep these parent credentials coordinator-only." with:

```markdown
Branching requires a token-protected parent; a `--no-token` server is rejected.
The coordinator needs the parent URL (`localhost` or `127.0.0.1`) and a token
file from the user. `branch.py` resolves the parent session itself from
`--parent-notebook`, an exact `--parent-session`, or the lone open session, and
errors (listing sessions) instead of guessing. Keep parent credentials
coordinator-only.
```

2. Replace the paragraph beginning "Before branch commands, arrange `MARIMO_TOKEN`" through "transient `curl` argv." with:

```markdown
Pass the parent token as `--token-file PATH` (or set `MARIMO_TOKEN_FILE`) on
`start` and `add`; avoid `--token` and inline environment assignments. Tokens
reach `curl` through a private header file, not process arguments.
```

3. Change the parent `help(cm)` example to:

```bash
bash /absolute/path/to/marimo-pair/scripts/execute-code.sh \
  --url http://localhost:2718 --file /abs/path/notebook.py \
  --token-file /path/to/token.txt \
  -c "import marimo._code_mode as cm; help(cm)"
```

4. Change the `start` example and the sentence after it to:

````markdown
After it succeeds, start one private worker per branch agent with a single
export of only the requested names:

```bash
python /absolute/path/to/marimo-pair/scripts/branch.py start \
  --parent-url http://localhost:2718 --parent-notebook /abs/path/notebook.py \
  --token-file /path/to/token.txt --count 3 --names records latest_model
```

`--count N` (1–16) prints a JSON list of workers sharing one snapshot; startup
is all-or-nothing. Once started, workers fail independently.
````

5. Change the `add` example to `branch.py add --worker WORKER_ID --token-file /path/to/token.txt --names new_value`, and replace "The coordinator supplies the current parent session ID (browser reconnects can change it) and runs `add`. The controller revalidates that session against the worker's original parent notebook path." with "The coordinator runs `add`, which resolves the parent's current session for the worker's notebook and revalidates it."

6. In the paragraph "Branch agents receive only `branch.py run`...", replace "parent token in transient `curl` argv during export" with "parent token while it is held by the coordinator's child processes during export".

- [ ] **Step 5: Verify docs**

Run:

```bash
wc -l skills/marimo-pair/SKILL.md
grep -rn "curl.*argv\|curl\` argv\|PARENT_SESSION\|separately approved" skills/marimo-pair/SKILL.md skills/marimo-pair/reference/
python3 skills/marimo-pair/scripts/branch.py start --help
```

Expected: SKILL.md < 500 lines; the grep prints nothing; `--help` shows descriptions for every flag including `--count` and `--token-file`.

- [ ] **Step 6: Full verification**

Run: `python3 -B -m unittest tests.test_execute_code_transport tests.test_branch tests.test_branch_state -v`
Expected: only the 3 baseline failures.

- [ ] **Step 7: Commit**

```bash
git add skills/marimo-pair/SKILL.md skills/marimo-pair/reference/execution-context.md skills/marimo-pair/reference/finding-marimo.md
git commit -m "docs: document token files, notebook-based session lookup, and start --count

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
