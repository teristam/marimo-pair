"""Opt-in end-to-end lifecycle checks against the installed marimo CLI."""

import concurrent.futures
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "marimo-pair" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import branch
import branch_state
from branch import ParentTarget, WorkerManager

RUN_INTEGRATION = os.environ.get("MARIMO_PAIR_RUN_INTEGRATION") == "1"
REPO_ROOT = Path(__file__).parents[1].resolve()


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _auth_request(url, token, *, session=None, method="GET", data=None, timeout=5):
    headers = {"Authorization": f"Bearer {token}"}
    if session:
        headers["Marimo-Session-Id"] = session
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=timeout)


class ParentServer:
    def __init__(self, root: Path, notebook: Path, marker: Path):
        self.root = root
        self.notebook = notebook
        self.marker = marker
        self.port = _port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.token = "parent-test-token-" + os.urandom(8).hex()
        self.token_path = root / "parent-token"
        self.token_path.write_text(self.token, encoding="utf-8")
        try:
            self.token_path.chmod(0o600)
        except OSError:
            pass
        self.log_path = root / "parent.log"
        self.log = None
        self.previous_token = os.environ.get("MARIMO_TOKEN")
        env = os.environ.copy()
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "MARIMO_PAIR_MARKER_PATH": str(marker),
            "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        })
        command = [
            sys.executable, "-m", "marimo", "-q", "edit", str(notebook),
            "--headless", "--host", "127.0.0.1", "--port", str(self.port),
            "--token-password-file", str(self.token_path), "--no-skew-protection",
            "--no-sandbox", "--skip-update-check",
        ]
        with self.log_path.open("wb") as log:
            self.process = subprocess.Popen(
                command, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, close_fds=True)
        os.environ["MARIMO_TOKEN"] = self.token
        self.session = "branch-parent-" + os.urandom(8).hex()
        self.sse = None
        self.code_mode_initialized = False

    def start(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"Parent server exited: {self.log_path.read_text(errors='replace')}")
            try:
                with _auth_request(self.url + "/api/sessions", self.token, timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError(f"Parent server did not start: {self.log_path.read_text(errors='replace')}")
        request = urllib.request.Request(
            self.url + "/sse?session_id=" + self.session,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.sse = urllib.request.urlopen(request, timeout=30)
        branch.wait_for_kernel_ready(self.sse, timeout=30)
        # Explicitly instantiate before polling status; edit startup can report idle before the app ran.
        with _auth_request(
            self.url + "/api/kernel/instantiate", self.token, session=self.session,
            method="POST", data=b'{"objectIds": [], "values": [], "autoRun": true}', timeout=10,
        ) as response:
            self.assert_http_ok(response)
        deadline = time.monotonic() + 30
        last_status = None
        while time.monotonic() < deadline:
            try:
                with _auth_request(
                    self.url + "/api/kernel/status", self.token, session=self.session, timeout=5,
                ) as response:
                    last_status = json.loads(response.read())
            except Exception as error:
                last_status = {"error": str(error)}
            if (
                last_status.get("state") == "idle"
                and self.marker.exists()
                and self.marker.read_text(encoding="utf-8").splitlines() == ["started"]
            ):
                self.sse.close()
                self.sse = None
                self.execute("import marimo._code_mode as cm; help(cm)")
                self.code_mode_initialized = True
                return
            time.sleep(0.1)
        marker_text = self.marker.read_text(encoding="utf-8") if self.marker.exists() else "<absent>"
        log_text = self.log_path.read_text(encoding="utf-8", errors="replace")
        raise AssertionError(f"Parent did not instantiate to idle exactly once: status={last_status}, marker={marker_text!r}, log={log_text}")

    @staticmethod
    def assert_http_ok(response):
        if not 200 <= response.status < 300:
            raise AssertionError(f"unexpected HTTP status {response.status}")

    def execute(self, code: str) -> str:
        env = os.environ.copy()
        env["MARIMO_TOKEN"] = self.token
        completed = subprocess.run(
            [str(branch.resolve_bash()), str(REPO_ROOT / "skills" / "marimo-pair" / "scripts" / "execute-code.sh"),
             "--url", self.url, "--session", self.session, "-"],
            input=code, text=True, encoding="utf-8", capture_output=True, timeout=30, env=env, close_fds=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr or completed.stdout)
        return completed.stdout

    def close(self):
        if self.sse is not None:
            self.sse.close()
        try:
            if self.process.poll() is None:
                try:
                    with _auth_request(
                        self.url + "/api/kernel/shutdown", self.token, session=self.session,
                        method="POST", data=b"{}", timeout=2,
                    ):
                        pass
                except Exception:
                    pass
                self.process.wait(timeout=5)
        except Exception:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.previous_token is None:
            os.environ.pop("MARIMO_TOKEN", None)
        else:
            os.environ["MARIMO_TOKEN"] = self.previous_token


@unittest.skipUnless(RUN_INTEGRATION, "set MARIMO_PAIR_RUN_INTEGRATION=1 to run real-server integration tests")
class WorkerIntegrationTests(unittest.TestCase):
    def _write_notebook(self, root: Path, scenario: str) -> tuple[Path, Path, tuple[str, ...], tuple[str, ...]]:
        marker = root / "marker.txt"
        if scenario == "notebook":
            source = REPO_ROOT / "tests" / "fixtures" / "branch_parent.py"
            notebook = root / "parent-copy.py"
            shutil.copyfile(source, notebook)
            (root / ".env").write_text("PAIR_INTEGRATION_VALUE=notebook-dotenv\n", encoding="utf-8")
            return notebook, marker, (), (str((root / ".env").resolve()),)

        project = root / "project"
        project.mkdir()
        src = project / ("script-src" if scenario == "script" else "src")
        src.mkdir()
        module_name = "script_context" if scenario == "script" else "project_context"
        (src / f"{module_name}.py").write_text("VALUE = 'module-" + scenario + "'\n", encoding="utf-8")
        env_name = ".script.env" if scenario == "script" else ".env"
        (project / env_name).write_text("PAIR_INTEGRATION_VALUE=" + scenario + "-dotenv\n", encoding="utf-8")
        if scenario == "script":
            (project / "project-src").mkdir()
            (project / ".project.env").write_text("PAIR_INTEGRATION_VALUE=wrong-project-value\n", encoding="utf-8")
            (project / "pyproject.toml").write_text(
                '[tool.marimo.runtime]\npythonpath = ["project-src"]\ndotenv = [".project.env"]\n',
                encoding="utf-8",
            )
            header = (
                "# /// script\n# [tool.marimo.runtime]\n"
                f"# pythonpath = [{json.dumps(str(src.resolve()))}]\n"
                f'# dotenv = ["{env_name}"]\n# ///\n'
            )
        else:
            (project / "pyproject.toml").write_text(
                '[tool.marimo.runtime]\npythonpath = ["src"]\ndotenv = [".env"]\n',
                encoding="utf-8",
            )
            header = ""
        notebook = project / "parent.py"
        notebook.write_text(
            header
            + "import marimo\napp = marimo.App()\n\n"
            + "@app.cell\ndef _():\n"
            + "    import os\n    from pathlib import Path\n"
            + "    from tests.fixtures.branch_models import BranchModel\n"
            + f"    from {module_name} import VALUE\n"
            + "    _marker = Path(os.environ['MARIMO_PAIR_MARKER_PATH'])\n"
            + "    if not _marker.exists(): _marker.write_text('started\\n', encoding='utf-8')\n"
            + "    records = [1]\n    sample = BranchModel('parent', records)\n"
            + "    return BranchModel, VALUE, records, sample\n",
            encoding="utf-8",
        )
        if scenario == "script":
            return notebook, marker, (str(src.resolve()),), (str((project / env_name).resolve()),)
        return notebook, marker, (str(src.resolve()),), (str((project / env_name).resolve()),)

    def _start_parent(self, root: Path, scenario: str):
        notebook, marker, pythonpath, dotenv = self._write_notebook(root, scenario)
        parent = ParentServer(root, notebook, marker)
        try:
            parent.start()
        except BaseException:
            parent.close()
            raise
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["started"])
        return parent, notebook, marker, pythonpath, dotenv

    def _create_parent_cell(self, parent: ParentServer, source: str):
        nl = chr(10)
        code = (
            "import marimo._code_mode as cm" + nl
            + "async with cm.get_context() as _pair_ctx:" + nl
            + f"    _pair_id = _pair_ctx.create_cell({source!r})" + nl
            + "    _pair_ctx.run_cell(_pair_id)" + nl
            + "async with cm.get_context() as _pair_ctx:" + nl
            + "    print('PARENT_CELL_STATUS:' + str(_pair_ctx.cells[_pair_id].status))" + nl
        )
        output = parent.execute(code)
        if "PARENT_CELL_STATUS:idle" not in output:
            raise AssertionError(f"Parent test cell did not complete: {output}")

    def _create_worker_cell(self, manager: WorkerManager, worker_id: str, source: str):
        nl = chr(10)
        code = (
            "import marimo._code_mode as cm" + nl
            + "async with cm.get_context() as _pair_ctx:" + nl
            + f"    _pair_id = _pair_ctx.create_cell({source!r})" + nl
            + "    _pair_ctx.run_cell(_pair_id)" + nl
        )
        result = manager.execute(worker_id, code)
        if not result.success:
            raise AssertionError(f"Could not create worker fixture cell: {result.stderr or result.output}")

    def _worker_cell_ids(self, manager: WorkerManager, worker_id: str):
        nl = chr(10)
        code = (
            "import json as _pair_json" + nl
            + "import marimo._code_mode as cm" + nl
            + "async with cm.get_context() as _pair_ctx:" + nl
            + "    print('PAIR_CELL_IDS:' + _pair_json.dumps(sorted(_pair_ctx.cells.keys())))" + nl
        )
        result = manager.execute(worker_id, code)
        if not result.success:
            raise AssertionError(f"Could not inspect worker cells: {result.stderr or result.output}")
        marker = next(line for line in result.output.splitlines() if line.startswith("PAIR_CELL_IDS:"))
        return json.loads(marker.partition(":")[2])

    def _start_worker(self, manager: WorkerManager, parent: ParentServer, notebook: Path, names):
        captured = {}
        original_export = manager._export_parent

        def export_and_capture(record, target, selected):
            original_export(record, target, selected)
            captured["runtime"] = branch_state.read_parent_runtime(record.runtime_path)

        self.assertTrue(parent.code_mode_initialized, "parent code mode must initialize before exporting values")
        with mock.patch.object(manager, "_export_parent", side_effect=export_and_capture):
            worker = manager.start_worker(ParentTarget(parent.url, parent.session, notebook), names)
        return worker, captured["runtime"]

    def _verify_worker_context(self, manager: WorkerManager, worker_id: str, scenario: str):
        module_value = "_context.VALUE" if scenario in {"project", "script"} else "None"
        code = (
            "import json, os, sys, importlib.util\n"
            "from marimo._config.manager import get_default_config_manager\n"
            f"_manager = get_default_config_manager(current_path={str(manager.get_worker(worker_id).notebook_path)!r})\n"
            "_config = _manager.get_config(hide_secrets=False)\n"
            "_runtime = _config.get('runtime', {})\n"
            + ("import project_context as _context\n" if scenario == "project" else "")
            + ("import script_context as _context\n" if scenario == "script" else "")
            + f"print('BRANCH_CONTEXT:' + json.dumps({{'pythonpath': [os.path.abspath(p) for p in _runtime.get('pythonpath', [])], 'dotenv': [os.path.abspath(p) for p in _runtime.get('dotenv', [])], 'env': os.environ.get('PAIR_INTEGRATION_VALUE'), 'module': {module_value}, 'model_type': type(sample).__module__, 'sys_path': sys.path}}))\n"
        )
        result = manager.execute(worker_id, code)
        self.assertTrue(result.success, result.output)
        line = next((line for line in result.output.splitlines() if line.startswith("BRANCH_CONTEXT:")), None)
        self.assertIsNotNone(line, result.output)
        return json.loads(line.partition(":")[2])

    def test_project_runtime_context_and_worker_log_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, notebook, marker, pythonpath, dotenv = self._start_parent(root, "project")
            manager = WorkerManager(root_dir=root / "manager")
            self.assertTrue(manager.root_dir.is_relative_to(root))
            try:
                worker, captured = self._start_worker(manager, parent, notebook, ["records", "sample"])
                context = self._verify_worker_context(manager, worker.worker_id, "project")
                self.assertEqual(context["pythonpath"], list(captured.pythonpath))
                self.assertEqual(context["dotenv"], list(captured.dotenv))
                self.assertEqual(context["env"], "project-dotenv")
                self.assertEqual(context["module"], "module-project")
                self.assertEqual(context["model_type"], "tests.fixtures.branch_models")
                self.assertTrue(worker.temp_dir.is_relative_to(manager.temp_root))
                self.assertNotEqual(worker.notebook_path, notebook.resolve())
                self.assertNotIn(manager.read_token(worker), worker.log_path.read_text(encoding="utf-8", errors="replace"))
                self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["started"])
                manager.stop_worker(worker.worker_id)
                self.assertEqual(manager.forced_kills, 0)
            finally:
                manager.stop_all()
                parent.close()

    def test_notebook_adjacent_dotenv_and_parallel_isolated_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, notebook, marker, _, dotenv = self._start_parent(root, "notebook")
            before = hashlib.sha256(notebook.read_bytes()).hexdigest()

            def parent_cells():
                output = parent.execute(
                    "import json as _pair_json\n"
                    "import marimo._code_mode as cm\n"
                    "async with cm.get_context() as _pair_ctx:\n"
                    "    print('PARENT_CELLS:' + _pair_json.dumps(sorted(_pair_ctx.cells.keys())))\n"
                )
                marker_line = next(line for line in output.splitlines() if line.startswith("PARENT_CELLS:"))
                return json.loads(marker_line.partition(":")[2])

            graph_before = parent_cells()
            parent_values_before = parent.execute("print('PARENT_RECORDS:' + repr(records))")
            parent_record_line = next(line for line in parent_values_before.splitlines() if line.startswith("PARENT_RECORDS:"))
            self.assertEqual(parent_record_line, "PARENT_RECORDS:[1]")
            manager = WorkerManager(root_dir=root / "manager")
            self.assertTrue(manager.root_dir.is_relative_to(root))
            try:
                first, captured = self._start_worker(manager, parent, notebook, ["records", "sample"])
                second, _ = self._start_worker(manager, parent, notebook, ["records", "sample"])
                self.assertNotEqual(first.notebook_path, second.notebook_path)
                self.assertNotEqual(first.notebook_path, notebook.resolve())
                for worker in (first, second):
                    self.assertTrue(worker.temp_dir.is_relative_to(manager.temp_root))
                    self.assertNotIn(manager.read_token(worker), worker.log_path.read_text(encoding="utf-8", errors="replace"))
                first_code = "records.append(10)\nsample.values.append(11)\nprint('BRANCH_RECORDS:' + repr(records))"
                second_code = "records.append(20)\nsample.values.append(21)\nprint('BRANCH_RECORDS:' + repr(records))"
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda args: manager.execute(*args), [
                        (first.worker_id, first_code), (second.worker_id, second_code),
                    ]))
                self.assertTrue(all(result.success for result in results), results)
                self.assertIn("BRANCH_RECORDS:[1, 10, 11]", results[0].output)
                self.assertIn("BRANCH_RECORDS:[1, 20, 21]", results[1].output)
                self.assertNotIn("BRANCH_RECORDS:[1, 20, 21]", results[0].output)
                self.assertNotIn("BRANCH_RECORDS:[1, 10, 11]", results[1].output)
                context = self._verify_worker_context(manager, first.worker_id, "notebook")
                self.assertEqual(context["pythonpath"], list(captured.pythonpath))
                self.assertEqual(context["dotenv"], list(captured.dotenv))
                self.assertEqual(context["env"], "notebook-dotenv")
                self.assertEqual(context["model_type"], "tests.fixtures.branch_models")
                self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["started"])
                parent_values_after = parent.execute("print('PARENT_RECORDS:' + repr(records))")
                parent_record_line_after = next(line for line in parent_values_after.splitlines() if line.startswith("PARENT_RECORDS:"))
                self.assertEqual(parent_record_line_after, parent_record_line)
                self.assertEqual(hashlib.sha256(notebook.read_bytes()).hexdigest(), before)
                self.assertEqual(parent_cells(), graph_before)
                for worker in (first, second):
                    started = time.monotonic()
                    manager.stop_worker(worker.worker_id)
                    self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(manager.forced_kills, 0)
            finally:
                manager.stop_all()
                parent.close()

    def test_later_named_batches_collisions_and_failed_export_keep_worker_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, notebook, _, _, _ = self._start_parent(root, "notebook")
            manager = WorkerManager(root_dir=root / "manager")
            try:
                worker, _ = self._start_worker(manager, parent, notebook, ["records"])
                self._create_parent_cell(parent, "later_value = 731; shared_with_later_batch = records")
                manager.add_worker(worker.worker_id, parent.session, ["later_value"])
                value = manager.execute(worker.worker_id, "print(later_value)")
                self.assertTrue(value.success, value.stderr)
                self.assertIn("731", value.output)

                manager.add_worker(worker.worker_id, parent.session, ["sample"])
                alias = manager.execute(worker.worker_id, "print(sample.values is records)")
                self.assertTrue(alias.success, alias.stderr)
                self.assertIn("False", alias.output)

                self._create_parent_cell(parent, "agent_owned = 4")
                self._create_worker_cell(manager, worker.worker_id, "agent_owned = 4")
                with mock.patch.object(manager, "_validate_parent", wraps=manager._validate_parent) as validate_parent, mock.patch.object(
                    manager, "_export_parent", wraps=manager._export_parent
                ) as export_parent:
                    with self.assertRaises(branch.BranchError):
                        manager.add_worker(worker.worker_id, parent.session, ["agent_owned"])
                validate_parent.assert_not_called()
                export_parent.assert_not_called()
                self._create_parent_cell(parent, "restore_probe = 27")
                original_export = manager._export_parent

                def corrupt_batch(record, target, selected):
                    original_export(record, target, selected)
                    record.bundle_path.write_bytes(b"invalid pickle batch")

                cells_before_failed_restore = self._worker_cell_ids(manager, worker.worker_id)
                with mock.patch.object(manager, "_export_parent", side_effect=corrupt_batch):
                    with self.assertRaises(branch.BranchError):
                        manager.add_worker(worker.worker_id, parent.session, ["restore_probe"])
                cells_after_failed_restore = self._worker_cell_ids(manager, worker.worker_id)
                self.assertEqual(cells_after_failed_restore, cells_before_failed_restore)
                self.assertNotIn("restore_probe", manager._inspect_worker_names(worker))
                still_usable = manager.execute(worker.worker_id, "print(records)")
                self.assertTrue(still_usable.success, still_usable.stderr)

                self._create_parent_cell(parent, "not_pickleable = lambda: 1")
                with self.assertRaises(branch.BranchError) as failure:
                    manager.add_worker(worker.worker_id, parent.session, ["not_pickleable"])
                self.assertIn("not_pickleable", str(failure.exception))
                self.assertIn("function", str(failure.exception))
                usable = manager.execute(worker.worker_id, "print('still-usable')")
                self.assertTrue(usable.success, usable.stderr)
                self.assertIn("still-usable", usable.output)
            finally:
                manager.stop_all()
                parent.close()

    def test_cli_start_run_add_and_stop_are_separate_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, notebook, _, _, _ = self._start_parent(root, "notebook")
            manager_root = root / "cli-manager"
            env = os.environ.copy()
            env.update({"MARIMO_PAIR_HOME": str(manager_root), "MARIMO_TOKEN": parent.token,
                        "PYTHONDONTWRITEBYTECODE": "1"})
            script = SCRIPTS_DIR / "branch.py"

            def cli(*args, input_text=None, worker_agent=False):
                child_env = env.copy()
                if worker_agent:
                    child_env.pop("MARIMO_TOKEN", None)
                    child_env["PYTHONIOENCODING"] = "cp1252:strict"
                return subprocess.run(
                    [sys.executable, "-B", str(script), *args], input=input_text,
                    text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=60, env=child_env,
                )

            try:
                started = cli("start", "--parent-url", parent.url, "--parent-session", parent.session,
                              "--names", "records")
                self.assertEqual(started.returncode, 0, started.stderr[-2000:])
                record_data = json.loads(started.stdout)
                worker_id = record_data["worker_id"]
                self.assertTrue(WorkerManager(root_dir=manager_root).get_worker(worker_id).status == "running")

                ran = cli("run", "--worker", worker_id, "-", input_text="print('from-cli-worker 東京🚀')", worker_agent=True)
                self.assertEqual(ran.returncode, 0, ran.stderr[-2000:])
                self.assertEqual(ran.stdout, "from-cli-worker 東京🚀" + chr(10) * 2)

                self._create_parent_cell(parent, "cli_later = 'current-state'")
                added = cli("add", "--worker", worker_id, "--parent-session", parent.session,
                            "--names", "cli_later")
                self.assertEqual(added.returncode, 0, added.stderr[-2000:])
                self.assertIn("current state", added.stdout)
                ran_later = cli("run", "--worker", worker_id, "-", input_text="print(cli_later)", worker_agent=True)
                self.assertEqual(ran_later.returncode, 0, ran_later.stderr[-2000:])
                self.assertIn("current-state", ran_later.stdout)

                stopped = cli("stop", "--worker", worker_id)
                self.assertEqual(stopped.returncode, 0, stopped.stderr[-2000:])
                self.assertEqual(WorkerManager(root_dir=manager_root).list_workers(), [])
            finally:
                WorkerManager(root_dir=manager_root).stop_all()
                parent.close()

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
                self.assertNotEqual(workers[0]["worker_id"], workers[1]["worker_id"])
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
                self.assertEqual(ran_later.returncode, 0, ran_later.stderr[-2000:])
                self.assertIn("fresh", ran_later.stdout)

                for worker in workers:
                    stopped = cli("stop", "--worker", worker["worker_id"])
                    self.assertEqual(stopped.returncode, 0, stopped.stderr[-2000:])
                self.assertEqual(WorkerManager(root_dir=manager_root).list_workers(), [])
            finally:
                WorkerManager(root_dir=manager_root).stop_all()
                parent.close()

    def test_run_capability_persistent_cells_and_environment_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, notebook, _, _, _ = self._start_parent(root, "notebook")
            manager_root = root / "cli-manager"
            token_file = root / "parent-token"
            token_file.write_text(parent.token + "\n", encoding="utf-8")
            env = os.environ.copy()
            env.pop("MARIMO_TOKEN", None)
            env.pop("MARIMO_TOKEN_FILE", None)
            env.update({"MARIMO_PAIR_HOME": str(manager_root), "PYTHONDONTWRITEBYTECODE": "1"})
            script = SCRIPTS_DIR / "branch.py"

            def cli(*args):
                return subprocess.run(
                    [sys.executable, "-B", str(script), *args], text=True, encoding="utf-8",
                    errors="replace", capture_output=True, timeout=180, env=env,
                )

            # The branch agent's environment knows neither the registry root nor any token.
            agent_env = {key: value for key, value in os.environ.items()
                         if key not in {"MARIMO_TOKEN", "MARIMO_TOKEN_FILE", "MARIMO_PAIR_HOME"}}

            def agent(command, *args, input_text):
                return subprocess.run(
                    [*command, *args], input=input_text, text=True, encoding="utf-8", errors="replace",
                    capture_output=True, timeout=180, env=agent_env,
                )

            try:
                started = cli("start", "--parent-url", parent.url, "--parent-notebook", str(notebook),
                              "--token-file", str(token_file), "--names", "records", "--probe", "json")
                self.assertEqual(started.returncode, 0, started.stderr[-2000:])
                worker = json.loads(started.stdout)
                self.assertEqual(worker["environment"]["modules"]["json"], "unknown")
                self.assertIn("python", worker["environment"])
                command = worker["run_command"]

                first = agent(command, "--cell", "summary", "-", input_text="total = sum(records)\nprint(total)")
                self.assertEqual(first.returncode, 0, first.stderr[-2000:])
                self.assertIn("1", first.stdout.splitlines())
                # Re-running the same cell name replaces it instead of colliding with itself.
                second = agent(command, "--cell", "summary", "-", input_text="total = sum(records) * 10")
                self.assertEqual(second.returncode, 0, second.stderr[-2000:])
                later = agent(command, "-", input_text="print('total is', total)")
                self.assertEqual(later.returncode, 0, later.stderr[-2000:])
                self.assertIn("total is 10", later.stdout)
                # A second cell redefining the same name is rejected by the graph rules.
                clash = agent(command, "--cell", "other", "-", input_text="total = 0")
                self.assertNotEqual(clash.returncode, 0)
                failing = agent(command, "--cell", "broken", "-", input_text="partial = 1\nraise ValueError('nope')")
                self.assertNotEqual(failing.returncode, 0)
                self.assertIn("nope", failing.stdout + failing.stderr)
                self.assertIn("removed", failing.stdout + failing.stderr)
                # The failed new cell was deleted, so its names are free again.
                gone = agent(command, "-", input_text="print('partial' in globals())")
                self.assertIn("False", gone.stdout.splitlines())
                reused = agent(command, "--cell", "fixed", "-", input_text="partial = 5\nprint(partial)")
                self.assertEqual(reused.returncode, 0, reused.stderr[-2000:])
                self.assertIn("5", reused.stdout.splitlines())
                refused = agent(command, "stop", input_text="")
                self.assertEqual(refused.returncode, 2)

                stopped = cli("stop", "--worker", worker["worker_id"])
                self.assertEqual(stopped.returncode, 0, stopped.stderr[-2000:])
            finally:
                WorkerManager(root_dir=manager_root).stop_all()
                parent.close()

    def test_script_header_overrides_project_runtime_and_uses_project_root_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, notebook, _, pythonpath, dotenv = self._start_parent(root, "script")
            manager = WorkerManager(root_dir=root / "manager")
            self.assertTrue(manager.root_dir.is_relative_to(root))
            try:
                worker, captured = self._start_worker(manager, parent, notebook, ["records", "sample"])
                context = self._verify_worker_context(manager, worker.worker_id, "script")
                self.assertEqual(context["pythonpath"], list(captured.pythonpath))
                self.assertEqual(context["dotenv"], list(captured.dotenv))
                self.assertEqual(context["env"], "script-dotenv")
                self.assertEqual(context["module"], "module-script")
                self.assertEqual(context["model_type"], "tests.fixtures.branch_models")
                self.assertTrue(worker.temp_dir.is_relative_to(manager.temp_root))
                self.assertNotEqual(worker.notebook_path, notebook.resolve())
                self.assertNotIn(manager.read_token(worker), worker.log_path.read_text(encoding="utf-8", errors="replace"))
                manager.stop_worker(worker.worker_id)
                self.assertEqual(manager.forced_kills, 0)
            finally:
                manager.stop_all()
                parent.close()


if __name__ == "__main__":
    unittest.main()
