import json
import os
import platform
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "marimo-pair" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import branch
import branch_state
from branch import (
    BranchError,
    ParentTarget,
    WorkerManager,
    parse_sse_records,
    resolve_bash,
    redact_log_text,
    process_identity,
    process_matches,
)
from branch_state import BranchStateError


class SSETests(unittest.TestCase):
    def test_parses_json_sse_records_with_whitespace_and_multiline_data(self):
        events = list(parse_sse_records([
            "event: notification\r\n",
            'data:  {"op": "kernel-ready",\r\n',
            'data: "data": {}}  \r\n',
            "\r\n",
        ]))
        self.assertEqual(events[0].event, "notification")
        self.assertEqual(events[0].data["op"], "kernel-ready")

    def test_readiness_fails_fast_for_close_and_startup_error(self):
        for stream, expected in (
            (["event: close\n", 'data: {"reason":"closed"}\n', "\n"], "close"),
            (["data: {\"op\":\"kernel-startup-error\"}\n", "\n"], "startup"),
        ):
            with self.subTest(expected=expected), self.assertRaises(BranchError):
                branch.wait_for_kernel_ready(stream, timeout=0.2)

    def test_readiness_rejects_eof_and_uses_a_bounded_timeout(self):
        with self.assertRaises(BranchError):
            branch.wait_for_kernel_ready([], timeout=0.2)

        def slow_lines():
            time.sleep(0.12)
            yield 'data: {"op":"kernel-ready"}\n'
            yield "\n"

        started = time.monotonic()
        with self.assertRaises(BranchError):
            branch.wait_for_kernel_ready(slow_lines(), timeout=0.02)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_ready_notification_is_accepted(self):
        stream = ['data: {"op":"kernel-ready","data":{}}\n', "\n"]
        branch.wait_for_kernel_ready(stream, timeout=0.2)


class BashSelectionTests(unittest.TestCase):
    def test_windows_path_candidates_keep_order_skip_wsl_and_git_fallback(self):
        system32 = Path("C:/Windows/System32/bash.exe")
        sysnative = Path("C:/Windows/Sysnative/bash.exe")
        windowsapps = Path("C:/Users/u/AppData/Local/Microsoft/WindowsApps/bash.exe")
        bad = Path("C:/tools/bash.exe")
        git = Path("C:/Program Files/Git/bin/bash.exe")
        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.object(branch, "_path_bash_candidates", return_value=[system32, sysnative, windowsapps, bad]),
            mock.patch.object(branch, "_git_bash_candidate", return_value=git),
            mock.patch.object(branch, "_bash_ostype", side_effect=lambda path: "msys" if path == git else "linux-gnu"),
        ):
            self.assertEqual(resolve_bash(), git)

    def test_first_valid_windows_candidate_wins_after_skipping_wsl_launchers(self):
        system32 = Path("C:/Windows/System32/bash.exe")
        first = Path("C:/Git/bin/bash.exe")
        second = Path("C:/other/bin/bash.exe")
        calls = []

        def ostype(path):
            calls.append(path)
            return "msys" if path in {first, second} else "linux-gnu"

        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/u/AppData/Local"}),
            mock.patch.object(branch, "_path_bash_candidates", return_value=[system32, first, second]),
            mock.patch.object(branch, "_bash_ostype", side_effect=ostype),
        ):
            self.assertEqual(resolve_bash(), first)
        self.assertEqual(calls, [first])

    def test_override_is_first_but_must_be_validated(self):
        override = Path("C:/custom/bash.exe")
        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.dict(os.environ, {"MARIMO_PAIR_BASH": str(override)}),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(branch, "_bash_ostype", return_value="cygwin"),
        ):
            self.assertEqual(resolve_bash(), override)
        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.dict(os.environ, {"MARIMO_PAIR_BASH": str(override)}),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(branch, "_bash_ostype", return_value="linux-gnu"),
            self.assertRaises(BranchError),
        ):
            resolve_bash()

    def test_windows_requires_msys_or_cygwin_ostype_and_reports_missing_fallback(self):
        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(branch, "_path_bash_candidates", return_value=[Path("C:/bad/bash.exe")]),
            mock.patch.object(branch, "_git_bash_candidate", return_value=None),
            mock.patch.object(branch, "_bash_ostype", return_value="linux-gnu"),
            self.assertRaises(BranchError),
        ):
            resolve_bash()

    def test_non_windows_uses_normal_bash_lookup(self):
        bash = Path("/usr/bin/bash")
        with (
            mock.patch.object(branch.os, "name", "posix"),
            mock.patch.object(branch.shutil, "which", return_value=str(bash)),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(branch.os, "access", return_value=True),
        ):
            self.assertEqual(str(resolve_bash()).replace("\\", "/"), "/usr/bin/bash")

    def test_preflight_checks_jq_and_curl_inside_selected_bash(self):
        with mock.patch.object(branch, "_bash_run", return_value="") as run:
            branch.preflight_bash(Path("/bin/bash"))
        command = run.call_args.args[1]
        self.assertIn("jq", command)
        self.assertIn("curl", command)
        with mock.patch.object(branch, "_bash_run", side_effect=BranchError("missing jq")):
            with self.assertRaises(BranchError):
                branch.preflight_bash(Path("/bin/bash"))


class RegistryAndIdentityTests(unittest.TestCase):
    def test_linux_identity_uses_creation_time_and_liveness_detects_zombies(self):
        def stat_line(state, start):
            tail = [state, *(["0"] * 18), str(start)]
            return f"123 (python) {' '.join(tail)}"

        with (
            mock.patch.object(branch.os, "name", "posix"),
            mock.patch.object(branch.sys, "platform", "linux"),
            mock.patch.object(branch.Path, "read_text", side_effect=[
                stat_line("S", 9876), stat_line("I", 9876),
                stat_line("Z", 9876), stat_line("Z", 9876),
            ]),
            mock.patch.object(branch, "_process_executable", return_value="/usr/bin/python"),
        ):
            sleeping = process_identity(123)
            idle = process_identity(123)
            self.assertEqual(sleeping, idle)
            self.assertIsNone(process_identity(123))
            self.assertFalse(branch._pid_alive_state(123))

    def test_macos_identity_ignores_mutable_state_and_liveness_detects_zombies(self):
        outputs = [
            "Mon Oct  6 12:34:56 2026 /opt/python/bin/python S\n",
            "Mon Oct  6 12:34:56 2026 /opt/python/bin/python I\n",
            "Mon Oct  6 12:34:56 2026 /opt/python/bin/python Z\n",
            "123 Z\n",
        ]
        with (
            mock.patch.object(branch.os, "name", "posix"),
            mock.patch.object(branch.sys, "platform", "darwin"),
            mock.patch.object(branch.subprocess, "run", side_effect=[
                mock.Mock(returncode=0, stdout=output) for output in outputs
            ]),
        ):
            running = process_identity(123)
            idle = process_identity(123)
            self.assertEqual(running, idle)
            self.assertIsNone(process_identity(123))
            self.assertFalse(branch._pid_alive_state(123))

    def test_windows_process_identity_and_liveness_reject_signaled_handle(self):
        signaled = mock.Mock()
        signaled.OpenProcess.return_value = 17
        signaled.GetProcessTimes.return_value = 1
        signaled.WaitForSingleObject.return_value = 0  # WAIT_OBJECT_0
        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.object(branch, "_windows_kernel32", return_value=signaled),
        ):
            self.assertIsNone(process_identity(123))
            self.assertFalse(branch._pid_alive_state(123))
            self.assertTrue(branch._original_process_gone(123, "win:0"))

        live = mock.Mock()
        live.OpenProcess.return_value = 17
        live.GetProcessTimes.return_value = 1
        live.WaitForSingleObject.return_value = 0x102  # WAIT_TIMEOUT
        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.object(branch, "_windows_kernel32", return_value=live),
        ):
            self.assertEqual(process_identity(123), "win:0")
            self.assertTrue(branch._pid_alive_state(123))

    def test_windows_wait_accepts_signaled_handle_with_no_live_identity(self):
        signaled = mock.Mock()
        signaled.OpenProcess.return_value = 17
        signaled.WaitForSingleObject.return_value = 0  # WAIT_OBJECT_0
        with (
            mock.patch.object(branch.os, "name", "nt"),
            mock.patch.object(branch, "_windows_kernel32", return_value=signaled),
        ):
            self.assertTrue(branch.wait_for_process(123, "win:0", 0.01))
        signaled.CloseHandle.assert_called_once_with(17)

    def test_early_windows_worker_exit_does_not_wait_or_force_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = WorkerManager(root_dir=Path(directory), startup_timeout=0.2)
            record = branch.WorkerRecord(
                worker_id="9" * 32, url="http://127.0.0.1:1234", session_id="s",
                token_path=Path(directory) / "token", notebook_path=Path(directory) / "n.py",
                temp_dir=Path(directory) / "worker", runtime_path=Path(directory) / "r.json",
                parent_url="http://127.0.0.1:4321", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="starting", server_pid=123, process_identity="win:0",
                imported_names=(), log_path=Path(directory) / "w.log",
                bundle_path=Path(directory) / "b.pickle", helper_path=Path(directory) / "h.py",
            )
            record.temp_dir.mkdir()
            process = mock.Mock(pid=123)
            process.poll.return_value = 1
            manager._processes[record.worker_id] = process
            signaled = mock.Mock()
            signaled.OpenProcess.return_value = 17
            signaled.GetProcessTimes.return_value = 1
            signaled.WaitForSingleObject.return_value = 0
            started = time.monotonic()
            with (
                mock.patch.object(branch.os, "name", "nt"),
                mock.patch.object(branch, "_windows_kernel32", return_value=signaled),
                mock.patch.object(manager, "_http_request", return_value=(401, b"")) as request,
                mock.patch.object(manager, "_terminate_if_same_process") as terminate,
            ):
                with self.assertRaises(BranchError):
                    manager._wait_fresh_server(record, "secret")
                self.assertTrue(manager._abort_worker(record))
            self.assertLess(time.monotonic() - started, 0.15)
            request.assert_not_called()
            terminate.assert_not_called()
            self.assertEqual(manager.forced_kills, 0)
            self.assertFalse(manager._record_path(record.worker_id).exists())

    def test_independent_registry_files_allow_parallel_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = WorkerManager(root_dir=Path(directory))
            records = [branch.WorkerRecord(
                worker_id=f"{i:032x}", url="http://127.0.0.1:1", session_id="s",
                token_path=Path(directory) / f"{i}.token", notebook_path=Path(directory) / f"{i}.py",
                temp_dir=Path(directory) / str(i), runtime_path=Path(directory) / f"{i}.json",
                parent_url="http://127.0.0.1:2", parent_notebook_path=Path(directory) / "parent.py",
                parent_session_id="p", status="starting", server_pid=None, process_identity=None,
                imported_names=(), log_path=Path(directory) / f"{i}.log",
                bundle_path=Path(directory) / f"{i}.pickle", helper_path=Path(directory) / f"{i}.py",
            ) for i in range(12)]
            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(manager._write_record, records))
            stored = [manager._read_record(path.stem) for path in manager.registry_dir.glob("*.json")]
            found = {record.worker_id for record in stored}
            self.assertEqual(found, {record.worker_id for record in records})
            self.assertEqual(len(list(manager.registry_dir.glob("*.json"))), 12)

    def test_process_identity_checks_pid_reuse(self):
        identity = process_identity(os.getpid())
        self.assertTrue(process_matches(os.getpid(), identity))
        self.assertFalse(process_matches(os.getpid(), "definitely-not-this-process"))

    def test_list_does_not_delete_an_active_provisional_start_record(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = WorkerManager(root_dir=Path(directory))
            record = branch.WorkerRecord(
                worker_id="d" * 32, url="http://127.0.0.1:1234", session_id="s",
                token_path=Path(directory) / "token", notebook_path=Path(directory) / "n.py",
                temp_dir=Path(directory) / "worker", runtime_path=Path(directory) / "r.json",
                parent_url="http://127.0.0.1:4321", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="starting", server_pid=None, process_identity=None,
                imported_names=(), log_path=Path(directory) / "w.log",
                bundle_path=Path(directory) / "b.pickle", helper_path=Path(directory) / "h.py",
                starter_pid=os.getpid(), starter_identity=process_identity(os.getpid()),
            )
            manager._write_record(record)
            self.assertEqual([item.worker_id for item in manager.list_workers()], [record.worker_id])
            self.assertTrue(manager._record_path(record.worker_id).exists())

    @unittest.skipUnless(os.name == "nt", "Windows process wait APIs are platform-specific")
    def test_windows_safe_wait_does_not_use_kill_zero(self):
        fake_kernel = mock.Mock()
        fake_kernel.OpenProcess.return_value = 0
        with (
            mock.patch.object(branch.os, "kill", side_effect=AssertionError("unsafe Windows probe")),
            mock.patch.object(branch.ctypes, "WinDLL", return_value=fake_kernel),
            mock.patch.object(branch, "_pid_alive_state", return_value=False),
        ):
            self.assertTrue(branch.wait_for_process(os.getpid(), "identity", 0.01))


class WorkerManagerUnitTests(unittest.TestCase):
    def test_add_rejects_all_private_names_and_existing_worker_names_before_parent_contact(self):
        manager = WorkerManager(root_dir=Path(tempfile.mkdtemp()))
        record = mock.Mock(worker_id="a" * 32, imported_names=("records",), status="running")
        with mock.patch.object(manager, "_read_record", return_value=record), mock.patch.object(
            manager, "_validate_worker"
        ), mock.patch.object(manager, "_inspect_worker_names", return_value={"from_agent"}), mock.patch.object(
            manager, "_validate_parent"
        ) as validate_parent, mock.patch.object(manager, "_export_parent") as export:
            for names in (["records"], ["from_agent"], ["_private"], ["__dunder__"]):
                with self.subTest(names=names), self.assertRaises((BranchError, ValueError)):
                    manager.add_worker(record.worker_id, "fresh-session", names)
        validate_parent.assert_not_called()
        export.assert_not_called()

    def test_worker_definition_inspector_reads_graph_cell_definitions(self):
        manager = WorkerManager(root_dir=Path(tempfile.mkdtemp()))
        record = mock.Mock(worker_id="e" * 32)
        output = 'MARIMO_PAIR_WORKER_DEFS:["agent_owned"]'
        with mock.patch.object(manager, "_execute_record", return_value=mock.Mock(success=True, output=output)) as execute:
            self.assertEqual(manager._inspect_worker_names(record), {"agent_owned"})
        generated = execute.call_args.args[1]
        self.assertIn("_bp_ctx.graph.cells.values()", generated)
        self.assertNotIn("getattr(_bp_cell,", generated)

    def test_add_validates_fresh_parent_session_path_before_export(self):
        manager = WorkerManager(root_dir=Path(tempfile.mkdtemp()))
        record = mock.Mock(worker_id="b" * 32, imported_names=("records",), status="running")
        with mock.patch.object(manager, "_read_record", return_value=record), mock.patch.object(
            manager, "_validate_worker"
        ), mock.patch.object(manager, "_inspect_worker_names", return_value=set()), mock.patch.object(
            manager, "_validate_parent", side_effect=BranchError("stale session")
        ) as validate, mock.patch.object(manager, "_export_parent") as export:
            with self.assertRaises(BranchError):
                manager.add_worker(record.worker_id, "stale-session", ["new_value"])
        validate.assert_called_once()
        export.assert_not_called()

    def test_failed_add_rolls_back_new_cell_and_artifacts(self):
        manager = WorkerManager(root_dir=Path(tempfile.mkdtemp()))
        record = mock.Mock(worker_id="c" * 32, imported_names=("records",), status="running")
        record.temp_dir = Path(tempfile.mkdtemp())
        record.parent_notebook_path = record.temp_dir / "parent.py"
        record.parent_url = "http://127.0.0.1:1234"
        record.bundle_path = record.temp_dir / "values.pickle"
        record.runtime_path = record.temp_dir / "runtime.json"
        record.helper_path = record.temp_dir / "branch_state.py"
        record.bundle_path.write_bytes(b"bundle")
        record.runtime_path.write_text("{}", encoding="utf-8")
        runtime = branch_state.ParentRuntime(
            notebook_path=str(record.temp_dir / "parent.py"), config_sources={}, cwd=str(record.temp_dir),
            sys_path=(), python_executable=sys.executable, python_version=platform.python_version(),
            pythonpath=(), dotenv=(), sandbox_context={}, dependency_context={},
        )
        with mock.patch.object(manager, "_read_record", return_value=record), mock.patch.object(
            manager, "_validate_worker"
        ), mock.patch.object(manager, "_inspect_worker_names", return_value=set()), mock.patch.object(
            manager, "_validate_parent", return_value=str(record.temp_dir / "parent.py")
        ), mock.patch.object(manager, "_export_parent"), mock.patch.object(manager, "_write_record"), mock.patch.object(
            branch, "read_parent_runtime", return_value=runtime
        ), mock.patch.object(manager, "_validate_runtime_code", return_value="verify runtime"), mock.patch.object(
            manager, "_delete_worker_cell"
        ) as delete_cell, mock.patch.object(manager, "_execute_record", side_effect=[
            mock.Mock(success=True, output="", stderr=""),
            mock.Mock(
                success=True,
                output='MARIMO_PAIR_BOOTSTRAP:{"status":"exception","names":["new_value"],"cell_id":"failed-cell"}',
                stderr="",
            ),
        ]):
            with self.assertRaises(BranchError):
                manager.add_worker(record.worker_id, "fresh-session", ["new_value"])
        delete_cell.assert_called_once_with(record, "failed-cell")
        self.assertFalse(record.bundle_path.exists())
        self.assertFalse(record.runtime_path.exists())

    def test_start_cli_rejects_private_names_before_parent_contact(self):
        with mock.patch.object(branch.WorkerManager, "parent_target") as parent_target, mock.patch.object(
            branch.sys, "stderr"
        ):
            self.assertEqual(branch.main([
                "start", "--parent-url", "http://127.0.0.1:1234", "--parent-session", "s",
                "--names", "_private",
            ]), 1)
        parent_target.assert_not_called()

    def test_cli_start_refuses_process_local_sse_drainer(self):
        worker_id = "f" * 32
        record = mock.Mock(worker_id=worker_id, url="http://127.0.0.1:1234", session_id="worker-session", status="running")

        def initialize(manager):
            manager._drainers = {worker_id: (mock.Mock(), mock.Mock(), mock.Mock())}

        with mock.patch.object(branch.WorkerManager, "__init__", autospec=True, side_effect=initialize), mock.patch.object(
            branch.WorkerManager, "parent_target", return_value=ParentTarget("http://127.0.0.1:4321", "parent", Path("parent.py"))
        ), mock.patch.object(branch.WorkerManager, "start_worker", return_value=record), mock.patch.object(
            branch.WorkerManager, "stop_worker"
        ) as stop_worker, mock.patch.object(branch.sys, "stderr"):
            result = branch.main([
                "start", "--parent-url", "http://127.0.0.1:4321", "--parent-session", "parent",
                "--names", "records",
            ])
        self.assertEqual(result, 1)
        stop_worker.assert_called_once_with(worker_id)

    def test_run_cli_routes_only_worker_id_and_uses_stdin(self):
        source = "print('worker only')"
        with mock.patch.object(branch.WorkerManager, "execute", return_value=mock.Mock(success=True, output="done", stderr="", returncode=0)) as execute, mock.patch.object(
            branch.sys, "stdin", mock.Mock(read=mock.Mock(return_value=source), buffer=None)
        ), mock.patch.object(branch.sys, "stdout") as stdout:
            self.assertEqual(branch.main(["run", "--worker", "d" * 32, "-"]), 0)
        execute.assert_called_once_with("d" * 32, source)
        with mock.patch.object(branch.sys, "stderr"), self.assertRaises(SystemExit):
            branch.main(["run", "--worker", "d" * 32, "--url", "http://127.0.0.1"])

    def _manager(self, root: Path) -> WorkerManager:
        return WorkerManager(root_dir=root, startup_timeout=0.2, request_timeout=0.2)

    def test_parent_validation_rejects_remote_and_path_mismatch(self):
        manager = self._manager(Path(tempfile.mkdtemp()))
        with self.assertRaises(BranchError):
            manager._validate_parent_url("https://example.com")

    def test_parent_session_must_map_to_the_requested_notebook(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            notebook = Path(directory) / "parent.py"
            other = Path(directory) / "other.py"
            notebook.write_text("", encoding="utf-8")
            other.write_text("", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}),
                mock.patch.object(manager, "_validate_parent_url", return_value="http://127.0.0.1:1234"),
                mock.patch.object(manager, "_sessions", side_effect=[(401, None), (200, {"s": {"path": str(other)}})]),
            ):
                with self.assertRaises(BranchError):
                    manager._validate_parent(ParentTarget("http://127.0.0.1:1234", "s", notebook))

    def test_lost_worker_process_identity_prevents_any_http_contact(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            record = branch.WorkerRecord(
                worker_id="f" * 32, url="http://127.0.0.1:1234", session_id="s",
                token_path=Path(directory) / "token", notebook_path=Path(directory) / "n.py",
                temp_dir=Path(directory), runtime_path=Path(directory) / "r.json",
                parent_url="http://127.0.0.1:4321", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="running", server_pid=456, process_identity="old-process",
                imported_names=(), log_path=Path(directory) / "w.log",
                bundle_path=Path(directory) / "b.pickle", helper_path=Path(directory) / "h.py",
            )
            with (
                mock.patch.object(branch, "process_matches", return_value=False),
                mock.patch.object(manager, "_http_request") as request,
            ):
                with self.assertRaises(BranchError):
                    manager._validate_worker(record)
            request.assert_not_called()
            self.assertEqual(manager.get_worker(record.worker_id).status, "stopping")

    def test_execute_script_uses_explicit_url_session_and_utf8_stdin_never_dash_c(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            record = branch.WorkerRecord(
                worker_id="w", url="http://127.0.0.1:1234", session_id="s",
                token_path=Path(directory) / "token", notebook_path=Path(directory) / "n.py",
                temp_dir=Path(directory), runtime_path=Path(directory) / "r.json",
                parent_url="http://127.0.0.1:1235", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="running", server_pid=os.getpid(),
                process_identity=process_identity(os.getpid()), imported_names=(), log_path=Path(directory) / "w.log",
                bundle_path=Path(directory) / "b.pickle", helper_path=Path(directory) / "h.py",
            )
            record.token_path.write_text("secret", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}),
                mock.patch.object(manager, "_validate_worker", return_value=None),
                mock.patch.object(manager, "_bash", return_value=Path("/bin/bash")),
                mock.patch.object(branch.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="ok", stderr="")) as run,
            ):
                source = "print('non-ASCII: 🚀')"
                result = manager.execute("w", source, record=record)
            command = run.call_args.args[0]
            self.assertIn("--url", command)
            self.assertIn(record.url, command)
            self.assertIn("--session", command)
            self.assertIn(record.session_id, command)
            self.assertEqual(command[-1], "-")
            self.assertNotIn("-c", command)
            self.assertEqual(run.call_args.kwargs["input"], source)
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["env"]["MARIMO_TOKEN"], "secret")
            self.assertNotEqual(run.call_args.kwargs["env"]["MARIMO_TOKEN"], "parent-secret")
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(result.output, "ok")

    def test_start_launch_arguments_and_child_environment_are_hardened(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            token = "parent-secret-never-persist"
            record = branch.WorkerRecord(
                worker_id="c" * 32, url="http://127.0.0.1:4567", session_id="s",
                token_path=Path(directory) / "token", notebook_path=Path(directory) / "worker.py",
                temp_dir=Path(directory), runtime_path=Path(directory) / "runtime.json",
                parent_url="http://127.0.0.1:2345", parent_notebook_path=Path(directory) / "parent.py",
                parent_session_id="p", status="starting", server_pid=None, process_identity=None,
                imported_names=(), log_path=Path(directory) / "worker.log",
                bundle_path=Path(directory) / "bundle", helper_path=Path(directory) / "helper.py",
            )
            record.token_path.write_text("worker-secret", encoding="utf-8")
            runtime = branch_state.ParentRuntime(
                notebook_path=str(record.parent_notebook_path), config_sources={}, cwd=directory,
                sys_path=(), python_executable=sys.executable, python_version=platform.python_version(),
                pythonpath=(), dotenv=(), sandbox_context={}, dependency_context={},
            )
            process = mock.Mock(pid=os.getpid())
            process.poll.return_value = None
            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": token}),
                mock.patch.object(branch.subprocess, "Popen", return_value=process) as popen,
                mock.patch.object(branch, "process_identity", return_value="test-identity"),
            ):
                manager._spawn(record, runtime)
            command = popen.call_args.args[0]
            self.assertEqual(command[3:5], ["-q", "edit"])
            self.assertIn("--no-skew-protection", command)
            self.assertIn("--no-sandbox", command)
            self.assertIn("--skip-update-check", command)
            self.assertIn("--token-password-file", command)
            self.assertNotIn("--session-ttl", command)
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["stdin"], branch.subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], branch.subprocess.STDOUT)
            self.assertEqual(Path(kwargs["stdout"].name), record.log_path)
            self.assertEqual(kwargs["cwd"], runtime.cwd)
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertNotIn("MARIMO_TOKEN", kwargs["env"])
            if os.name == "nt":
                self.assertIn("creationflags", kwargs)
            else:
                self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn(token, json.dumps(record.to_dict()))
            self.assertNotIn(token, record.log_path.read_text(encoding="utf-8") if record.log_path.exists() else "")

    def test_inaccessible_required_runtime_path_is_rejected_before_popen(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            notebook = Path(directory) / "parent.py"
            notebook.write_text("", encoding="utf-8")
            missing = Path(directory) / "missing-import-root"

            def export(record, parent, names):
                fake = {
                    "notebook_path": str(notebook.resolve()), "config_sources": {}, "cwd": directory,
                    "sys_path": [], "python_executable": sys.executable,
                    "python_version": platform.python_version(), "pythonpath": [str(missing)],
                    "dotenv": [], "sandbox_context": {}, "dependency_context": {
                        "implementation": platform.python_implementation(), "python_prefix": sys.prefix,
                        "base_prefix": sys.base_prefix,
                    },
                }
                record.runtime_path.write_text(json.dumps(fake), encoding="utf-8")
                record.bundle_path.write_bytes(b"values")

            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}),
                mock.patch.object(manager, "_validate_parent", return_value=str(notebook.resolve())),
                mock.patch.object(manager, "_export_parent", side_effect=export),
                mock.patch.object(branch.subprocess, "Popen") as popen,
            ):
                with self.assertRaises(BranchStateError):
                    manager.start_worker(ParentTarget("http://127.0.0.1:1234", "p", notebook), ["records"])
            popen.assert_not_called()
            self.assertEqual(manager.list_workers(), [])

    def test_parent_export_subprocess_uses_utf8_for_non_ascii_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Zoë 🚀"
            root.mkdir()
            manager = self._manager(root)
            notebook = root / "parent-🚀.py"
            notebook.write_text("", encoding="utf-8")
            record = branch.WorkerRecord(
                worker_id="8" * 32, url="http://127.0.0.1:4567", session_id="s",
                token_path=root / "token", notebook_path=root / "worker.py",
                temp_dir=root, runtime_path=root / "runtime.json",
                parent_url="http://127.0.0.1:2345", parent_notebook_path=notebook,
                parent_session_id="p", status="starting", server_pid=None, process_identity=None,
                imported_names=("records",), log_path=root / "worker.log",
                bundle_path=root / "bundle.pickle", helper_path=root / "helper.py",
            )
            def run(command, **kwargs):
                record.runtime_path.write_text("{}", encoding="utf-8")
                record.bundle_path.write_bytes(b"values")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}),
                mock.patch.object(manager, "_bash", return_value=Path("/bin/bash")),
                mock.patch.object(branch.subprocess, "run", side_effect=run) as subprocess_run,
            ):
                manager._export_parent(record, ParentTarget(record.parent_url, "p", notebook), ["records"])
            self.assertEqual(subprocess_run.call_args.kwargs["encoding"], "utf-8")
            self.assertIn("🚀", subprocess_run.call_args.kwargs["input"])

    def test_stop_worker_terminates_verified_process_after_session_validation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            worker_dir = Path(directory) / "worker"
            worker_dir.mkdir()
            record = branch.WorkerRecord(
                worker_id="7" * 32, url="http://127.0.0.1:1111", session_id="s",
                token_path=worker_dir / "token", notebook_path=worker_dir / "n.py",
                temp_dir=worker_dir, runtime_path=worker_dir / "r.json",
                parent_url="http://127.0.0.1:2222", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="running", server_pid=123, process_identity="created-here",
                imported_names=(), log_path=worker_dir / "w.log",
                bundle_path=worker_dir / "b.pickle", helper_path=worker_dir / "h.py",
            )
            record.token_path.write_text("token", encoding="utf-8")
            manager._write_record(record)
            with (
                mock.patch.object(manager, "_validate_worker", side_effect=BranchError("session check failed")),
                mock.patch.object(manager, "_http_request") as request,
                mock.patch.object(branch, "process_matches", return_value=True),
                mock.patch.object(branch, "_original_process_gone", return_value=False),
                mock.patch.object(manager, "_terminate_if_same_process", return_value=True) as terminate,
                self.assertRaises(BranchError),
            ):
                manager.stop_worker(record.worker_id)
            request.assert_not_called()
            terminate.assert_called_once_with(record)
            self.assertFalse(manager._record_path(record.worker_id).exists())
            self.assertFalse(record.temp_dir.exists())

    def test_windows_stop_cleans_up_after_taskkill_and_signaled_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            worker_dir = Path(directory) / "worker"
            worker_dir.mkdir()
            record = branch.WorkerRecord(
                worker_id="5" * 32, url="http://127.0.0.1:1111", session_id="s",
                token_path=worker_dir / "token", notebook_path=worker_dir / "n.py",
                temp_dir=worker_dir, runtime_path=worker_dir / "r.json",
                parent_url="http://127.0.0.1:2222", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="running", server_pid=123, process_identity="win:0",
                imported_names=(), log_path=worker_dir / "w.log",
                bundle_path=worker_dir / "b.pickle", helper_path=worker_dir / "h.py",
            )
            record.token_path.write_text("token", encoding="utf-8")
            manager._write_record(record)
            exited = False
            kernel = mock.Mock()
            kernel.OpenProcess.return_value = 17
            kernel.WaitForSingleObject.side_effect = lambda *_: 0 if exited else 0x102

            def taskkill(command, **kwargs):
                nonlocal exited
                self.assertEqual(command[:4], ["taskkill", "/T", "/F", "/PID"])
                exited = True
                return mock.Mock(returncode=0)

            with (
                mock.patch.object(branch.os, "name", "nt"),
                mock.patch.object(branch, "_windows_kernel32", return_value=kernel),
                mock.patch.object(branch, "process_identity", side_effect=lambda pid: None if exited else "win:0"),
                mock.patch.object(branch.subprocess, "run", side_effect=taskkill) as run,
                mock.patch.object(manager, "_validate_worker", side_effect=BranchError("session check failed")),
                mock.patch.object(manager, "_http_request") as request,
                self.assertRaises(BranchError),
            ):
                manager.stop_worker(record.worker_id)
            run.assert_called_once()
            request.assert_not_called()
            self.assertEqual(manager.forced_kills, 1)
            self.assertFalse(manager._record_path(record.worker_id).exists())
            self.assertFalse(worker_dir.exists())

    def test_stop_worker_keeps_record_when_safe_termination_is_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            worker_dir = Path(directory) / "worker"
            worker_dir.mkdir()
            record = branch.WorkerRecord(
                worker_id="6" * 32, url="http://127.0.0.1:1111", session_id="s",
                token_path=worker_dir / "token", notebook_path=worker_dir / "n.py",
                temp_dir=worker_dir, runtime_path=worker_dir / "r.json",
                parent_url="http://127.0.0.1:2222", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="running", server_pid=123, process_identity="created-here",
                imported_names=(), log_path=worker_dir / "w.log",
                bundle_path=worker_dir / "b.pickle", helper_path=worker_dir / "h.py",
            )
            record.token_path.write_text("token", encoding="utf-8")
            manager._write_record(record)
            with (
                mock.patch.object(manager, "_validate_worker", side_effect=BranchError("session check failed")),
                mock.patch.object(manager, "_http_request") as request,
                mock.patch.object(branch, "process_matches", return_value=True),
                mock.patch.object(branch, "_original_process_gone", return_value=False),
                mock.patch.object(manager, "_terminate_if_same_process", return_value=False),
                self.assertRaises(BranchError),
            ):
                manager.stop_worker(record.worker_id)
            request.assert_not_called()
            self.assertTrue(manager._record_path(record.worker_id).exists())
            self.assertTrue(record.temp_dir.exists())

    def test_startup_cancellation_cleans_provisional_registry_and_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            notebook = Path(directory) / "parent.py"
            notebook.write_text("", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}),
                mock.patch.object(manager, "_validate_parent", return_value=str(notebook.resolve())),
                mock.patch.object(manager, "_export_parent", side_effect=KeyboardInterrupt()),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    manager.start_worker(ParentTarget("http://127.0.0.1:1234", "p", notebook), ["records"])
            self.assertEqual(manager.list_workers(), [])
            self.assertEqual(list(manager.workers_dir.iterdir()), [])
            self.assertEqual(list(manager.temp_root.iterdir()), [])

    def test_cancellation_after_spawn_stops_child_and_cleans_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            notebook = Path(directory) / "parent.py"
            notebook.write_text("", encoding="utf-8")
            process = mock.Mock(pid=987654)
            process.poll.return_value = None

            def export(record, parent, names):
                fake = {
                    "notebook_path": str(notebook.resolve()), "config_sources": {}, "cwd": directory,
                    "sys_path": [], "python_executable": sys.executable,
                    "python_version": platform.python_version(), "pythonpath": [], "dotenv": [],
                    "sandbox_context": {}, "dependency_context": {
                        "implementation": platform.python_implementation(), "python_prefix": sys.prefix,
                        "base_prefix": sys.base_prefix,
                    },
                }
                record.runtime_path.write_text(json.dumps(fake), encoding="utf-8")
                record.bundle_path.write_bytes(b"values")

            def spawn(record, runtime):
                record.server_pid = process.pid
                record.process_identity = "child-identity"
                manager._processes[record.worker_id] = process
                manager._write_record(record)
                return process

            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}),
                mock.patch.object(manager, "_validate_parent", return_value=str(notebook.resolve())),
                mock.patch.object(manager, "_export_parent", side_effect=export),
                mock.patch.object(manager, "_spawn", side_effect=spawn),
                mock.patch.object(manager, "_wait_fresh_server", side_effect=KeyboardInterrupt()),
                mock.patch.object(branch, "process_matches", return_value=False),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    manager.start_worker(ParentTarget("http://127.0.0.1:1234", "p", notebook), ["records"])
            process.wait.assert_called()
            self.assertEqual(list(manager.registry_dir.glob("*.json")), [])
            self.assertEqual(list(manager.temp_root.iterdir()), [])

    def test_startup_failure_cleans_registry_token_and_temp_data(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            notebook = Path(directory) / "parent.py"
            notebook.write_text("", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"MARIMO_TOKEN": "parent-secret"}),
                mock.patch.object(manager, "_validate_parent", return_value=str(notebook.resolve())),
                mock.patch.object(manager, "_export_parent", side_effect=RuntimeError("export failed")),
            ):
                with self.assertRaises(RuntimeError):
                    manager.start_worker(ParentTarget("http://127.0.0.1:10", "p", notebook), ["records"])
            self.assertEqual(manager.list_workers(), [])
            self.assertEqual(list(manager.workers_dir.iterdir()), [])

    def test_forced_termination_rechecks_creation_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            record = branch.WorkerRecord(
                worker_id="e" * 32, url="http://127.0.0.1:1234", session_id="s",
                token_path=Path(directory) / "token", notebook_path=Path(directory) / "n.py",
                temp_dir=Path(directory), runtime_path=Path(directory) / "r.json",
                parent_url="http://127.0.0.1:4321", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="running", server_pid=os.getpid(),
                process_identity="recorded-creation", imported_names=(), log_path=Path(directory) / "w.log",
                bundle_path=Path(directory) / "b.pickle", helper_path=Path(directory) / "h.py",
            )
            with (
                mock.patch.object(branch, "process_matches", return_value=True),
                mock.patch.object(branch, "process_identity", return_value="different-creation"),
                mock.patch.object(branch.subprocess, "run") as run,
            ):
                self.assertTrue(manager._terminate_if_same_process(record))
            run.assert_not_called()
            self.assertEqual(manager.forced_kills, 0)

    def test_log_redaction_covers_exact_tokens_and_access_token_query(self):
        self.assertNotIn("secret", redact_log_text("worker secret access_token=hello%2Fworld", ["secret"]))
        self.assertIn("access_token=[REDACTED]", redact_log_text("access_token=abc123", []))

    def test_shutdown_rejects_every_non_2xx_response(self):
        for status in (300, 401, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                manager = self._manager(Path(directory))
                record = branch.WorkerRecord(
                    worker_id="a" * 32, url="http://127.0.0.1:1111", session_id="s",
                    token_path=Path(directory) / "token", notebook_path=Path(directory) / "n.py",
                    temp_dir=Path(directory), runtime_path=Path(directory) / "r.json",
                    parent_url="http://127.0.0.1:2222", parent_notebook_path=Path(directory) / "p.py",
                    parent_session_id="p", status="running", server_pid=os.getpid(),
                    process_identity=process_identity(os.getpid()), imported_names=(), log_path=Path(directory) / "w.log",
                    bundle_path=Path(directory) / "b.pickle", helper_path=Path(directory) / "h.py",
                )
                record.token_path.write_text("token", encoding="utf-8")
                with (
                    mock.patch.object(manager, "_validate_worker"),
                    mock.patch.object(manager, "_http_request", return_value=(status, b"bad")),
                    mock.patch.object(manager, "_wait_for_server_exit", return_value=True),
                ):
                    with self.assertRaises(BranchError):
                        manager._shutdown(record)

    def test_fresh_manager_stops_worker_from_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            worker_dir = Path(directory) / "worker"
            worker_dir.mkdir()
            record = branch.WorkerRecord(
                worker_id="b" * 32, url="http://127.0.0.1:1111", session_id="s",
                token_path=worker_dir / "token", notebook_path=worker_dir / "n.py",
                temp_dir=worker_dir, runtime_path=worker_dir / "r.json",
                parent_url="http://127.0.0.1:2222", parent_notebook_path=Path(directory) / "p.py",
                parent_session_id="p", status="running", server_pid=os.getpid(),
                process_identity=process_identity(os.getpid()), imported_names=(), log_path=worker_dir / "w.log",
                bundle_path=worker_dir / "b.pickle", helper_path=worker_dir / "h.py",
            )
            record.token_path.write_text("token", encoding="utf-8")
            manager._write_record(record)
            fresh = self._manager(Path(directory))
            with (
                mock.patch.object(fresh, "_validate_worker"),
                mock.patch.object(fresh, "_http_request", return_value=(200, b'{"status":"ok"}')),
                mock.patch.object(fresh, "_wait_for_server_exit", return_value=True),
            ):
                fresh.stop_worker(record.worker_id)
            self.assertEqual(fresh.list_workers(), [])


if __name__ == "__main__":
    unittest.main()
