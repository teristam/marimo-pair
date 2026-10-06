import ast
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fixtures.branch_models import BranchModel

SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "marimo-pair" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import branch_state
from branch_state import (
    BranchStateError,
    build_parent_export_code,
    build_worker_bootstrap_cell,
    build_worker_notebook,
    export_named_values,
    read_batch,
    read_parent_runtime,
    validate_names,
    write_batch,
)


try:
    from marimo._environments import script_metadata as _script_metadata
except Exception:
    _script_metadata = None
MARIMO_AVAILABLE = _script_metadata is not None


class NameValidationTests(unittest.TestCase):
    def test_rejects_keywords_invalid_and_every_private_name(self):
        for name in ("class", "not-valid", "2fast", "_", "_x", "__dunder__"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_names([name])

    def test_rejects_existing_and_repeated_names(self):
        for names, existing in ((["value"], {"value"}), (["value", "value"], set())):
            with self.subTest(names=names), self.assertRaises(ValueError):
                validate_names(names, existing)

    def test_returns_names_in_request_order(self):
        self.assertEqual(validate_names(["records", "sample"]), ["records", "sample"])


class BatchSerializationTests(unittest.TestCase):
    def test_repeated_records_preserve_alias_identity(self):
        shared = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.pickle"
            write_batch({"left": shared, "right": shared}, path)
            values = read_batch(path)
        self.assertIs(values["left"], values["right"])

    def test_module_level_dataclass_roundtrips(self):
        expected = BranchModel("sample", [1, 2, 3])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.pickle"
            write_batch({"sample": expected}, path)
            values = read_batch(path)
        self.assertEqual(values["sample"], expected)
        self.assertIs(type(values["sample"]), BranchModel)

    def test_nested_main_definition_has_name_and_type_and_removes_artifact(self):
        notebook_class = type("NotebookOnly", (), {"__module__": "__main__"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.pickle"
            with self.assertRaises(BranchStateError) as caught:
                write_batch({"safe": [1], "nested": {"class": notebook_class()}}, path)
            self.assertIn("nested", str(caught.exception))
            self.assertIn("NotebookOnly", str(caught.exception))
            self.assertFalse(path.exists())

    def test_nested_ui_and_runtime_state_errors_are_attributable(self):
        ui_type = type("Text", (), {"__module__": "marimo._plugins.ui"})
        state_type = type("State", (), {"__module__": "marimo._runtime.state"})
        for name, value, type_name in (
            ("ui_value", {"nested": ui_type()}, "Text"),
            ("state_value", [state_type()], "State"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "batch.pickle"
                with self.assertRaises(BranchStateError) as caught:
                    write_batch({name: value}, path)
                self.assertIn(name, str(caught.exception))
                self.assertIn(type_name, str(caught.exception))
                self.assertFalse(path.exists())

    def test_export_deletes_both_artifacts_after_a_late_failure(self):
        notebook_class = type("NotebookOnly", (), {"__module__": "__main__"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notebook = root / "parent.py"
            notebook.write_text("", encoding="utf-8")
            bundle = root / "batch.pickle"
            metadata = root / "runtime.json"
            with mock.patch.object(branch_state, "_capture_parent_runtime", return_value={}):
                with self.assertRaises(BranchStateError):
                    export_named_values(
                        {"ok": [1], "bad": notebook_class()},
                        ["ok", "bad"],
                        bundle,
                        metadata,
                        notebook,
                    )
            self.assertFalse(bundle.exists())
            self.assertFalse(metadata.exists())


class RuntimeCaptureTests(unittest.TestCase):
    def _export_runtime(
        self,
        notebook: Path,
        root: Path,
        config: dict,
        script_runtime: dict | None = None,
        project_path: Path | None = None,
    ):
        bundle = root / "batch.pickle"
        metadata = root / "runtime.json"
        manager = SimpleNamespace(
            partials=(
                SimpleNamespace(
                    pyproject_path=project_path / "pyproject.toml" if project_path else None,
                    filename=str(notebook),
                ),
            ),
            user_config_mgr=None,
        )
        if MARIMO_AVAILABLE:
            export_named_values({}, [], bundle, metadata, notebook)
        else:
            with (
                mock.patch.object(branch_state, "_load_parent_config", return_value=(manager, config)),
                mock.patch.object(branch_state, "_script_runtime", return_value=script_runtime or {}),
            ):
                export_named_values({}, [], bundle, metadata, notebook)
        runtime = read_parent_runtime(metadata)
        return runtime, metadata

    def test_project_runtime_paths_are_absolute_and_dotenv_values_are_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            source = project / "src"
            source.mkdir(parents=True)
            dotenv = project / ".project.env"
            dotenv.write_text("MARIMO_PAIR_SECRET=super-secret-value\n", encoding="utf-8")
            (project / "pyproject.toml").write_text(
                '[tool.marimo.runtime]\npythonpath = ["src"]\ndotenv = [".project.env"]\n',
                encoding="utf-8",
            )
            notebook = project / "parent.py"
            notebook.write_text("", encoding="utf-8")
            with mock.patch.object(sys, "path", [*sys.path, str(source)]):
                runtime, metadata = self._export_runtime(
                    notebook,
                    root,
                    {"runtime": {"pythonpath": [str(source)], "dotenv": [str(dotenv)]}},
                    project_path=project,
                )
            self.assertIn(str(source.resolve()), runtime.pythonpath)
            self.assertEqual(runtime.dotenv, (str(dotenv.resolve()),))
            self.assertIn(str(project / "pyproject.toml"), runtime.config_sources.values())
            self.assertNotIn("super-secret-value", metadata.read_text(encoding="utf-8"))

    def test_no_project_uses_notebook_adjacent_dotenv_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dotenv = root / ".env"
            dotenv.write_text("PAIR_TEST_KEY=harmless\n", encoding="utf-8")
            notebook = root / "parent.py"
            notebook.write_text("", encoding="utf-8")
            runtime, _ = self._export_runtime(
                notebook, root, {"runtime": {"dotenv": [str(dotenv)]}}
            )
            self.assertEqual(runtime.dotenv, (str(dotenv.resolve()),))

    def test_script_paths_override_project_and_resolve_from_notebook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            notebook_dir = project / "notebooks"
            script_src = notebook_dir / "script-src"
            project_src = project / "project-src"
            script_src.mkdir(parents=True)
            project_src.mkdir(parents=True)
            (project / ".project.env").write_text("PROJECT_KEY=x\n", encoding="utf-8")
            (notebook_dir / ".script.env").write_text("SCRIPT_KEY=x\n", encoding="utf-8")
            (project / "pyproject.toml").write_text(
                '[tool.marimo.runtime]\npythonpath = ["project-src"]\ndotenv = [".project.env"]\n',
                encoding="utf-8",
            )
            notebook = notebook_dir / "parent.py"
            notebook.write_text(
                '# /// script\n'
                '# [tool.marimo.runtime]\n'
                '# pythonpath = ["script-src"]\n'
                '# dotenv = [".script.env"]\n'
                '# ///\n',
                encoding="utf-8",
            )
            with mock.patch.object(sys, "path", [*sys.path, str(script_src)]):
                runtime, _ = self._export_runtime(
                    notebook,
                    root,
                    {
                        "runtime": {
                            "pythonpath": ["script-src"],
                            "dotenv": [str((project / ".project.env").resolve())],
                        }
                    },
                    script_runtime={
                        "pythonpath": ["script-src"],
                        "dotenv": [".script.env"],
                    },
                    project_path=project,
                )
            self.assertEqual(runtime.pythonpath, (str(script_src.resolve()),))
            self.assertEqual(runtime.dotenv, (str((notebook_dir / ".script.env").resolve()),))

    def test_read_runtime_rejects_inaccessible_required_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notebook = root / "parent.py"
            notebook.write_text("", encoding="utf-8")
            (root / ".env").write_text("PAIR_TEST_KEY=harmless\n", encoding="utf-8")
            _, metadata = self._export_runtime(notebook, root, {"runtime": {}})
            data = json.loads(metadata.read_text(encoding="utf-8"))
            data["dotenv"] = [str(root / "missing.env")]
            metadata.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(BranchStateError):
                read_parent_runtime(metadata)


class CodeGenerationTests(unittest.TestCase):
    def test_worker_header_and_bootstrap_are_safe_and_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            dotenv = root / "values.env"
            dotenv.write_text("KEY=value\n", encoding="utf-8")
            runtime = branch_state.ParentRuntime(
                notebook_path=str(root / "parent.py"),
                config_sources={},
                cwd=str(root),
                sys_path=(str(root),),
                python_executable=sys.executable,
                python_version=sys.version,
                pythonpath=(str(source),),
                dotenv=(str(dotenv),),
                sandbox_context={},
                dependency_context={},
            )
            notebook = build_worker_notebook(runtime)
            self.assertIn("# /// script", notebook)
            block = notebook.split("# /// script\n", 1)[1].split("# ///", 1)[0]
            paths = {
                key.strip(): ast.literal_eval(value.strip())
                for key, value in (line[2:].split("=", 1) for line in block.splitlines()[1:])
            }
            self.assertEqual(paths["pythonpath"], [str(source)])
            self.assertEqual(paths["dotenv"], [str(dotenv)])
            self.assertIn("import marimo", notebook)
            self.assertIn("app = marimo.App()", notebook)
            helper = root / "helper.py"
            bundle = root / "batch.pickle"
            metadata = root / "runtime.json"
            bootstrap = build_worker_bootstrap_cell(
                ["records", "sample"], bundle, metadata, helper
            )
            self.assertIn("records = ", bootstrap)
            self.assertIn("sample = ", bootstrap)
            self.assertNotIn("globals().update", bootstrap)
            first = build_worker_bootstrap_cell(["records"], bundle, metadata, helper)
            self.assertNotEqual(bootstrap, first)
            self.assertIn("_marimo_pair_", bootstrap)
            compile(bootstrap, "<bootstrap>", "exec")

    def test_windows_paths_roundtrip_through_toml_header(self):
        runtime = branch_state.ParentRuntime(
            notebook_path=r'C:\Users\A B\notebook "quoted".py',
            config_sources={},
            cwd=r'C:\Users\A B',
            sys_path=(),
            python_executable=sys.executable,
            python_version=sys.version,
            pythonpath=(r'C:\Users\A B\src "quoted"',),
            dotenv=(r'C:\Users\A B\.env "quoted"',),
            sandbox_context={},
            dependency_context={},
        )
        header = build_worker_notebook(runtime)
        block = header.split("# /// script\n", 1)[1].split("# ///", 1)[0]
        paths = {
            key.strip(): ast.literal_eval(value.strip())
            for key, value in (line[2:].split("=", 1) for line in block.splitlines()[1:])
        }
        self.assertEqual(paths["pythonpath"], [r'C:\Users\A B\src "quoted"'])
        self.assertEqual(paths["dotenv"], [r'C:\Users\A B\.env "quoted"'])

    def test_unicode_windows_path_is_toml_safe(self):
        path = 'C:\\Users\\Zoë 🚀\\source "quoted"'
        runtime = branch_state.ParentRuntime(
            notebook_path="C:/parent.py",
            config_sources={},
            cwd="C:/",
            sys_path=(),
            python_executable=sys.executable,
            python_version=platform.python_version(),
            pythonpath=(path,),
            dotenv=(),
            sandbox_context={},
            dependency_context={},
        )
        header = build_worker_notebook(runtime)
        block = header.split("# /// script\n", 1)[1].split("# ///", 1)[0]
        paths = ast.literal_eval(block.splitlines()[1][len("# pythonpath = "):])
        self.assertEqual(paths, [path])

    @unittest.skipUnless(MARIMO_AVAILABLE, "marimo is not importable in this Python environment")
    def test_windows_paths_roundtrip_through_marimo_script_metadata(self):
        runtime = branch_state.ParentRuntime(
            notebook_path=r'C:\Users\A B\notebook "quoted".py',
            config_sources={},
            cwd=r'C:\Users\A B',
            sys_path=(),
            python_executable=sys.executable,
            python_version=sys.version,
            pythonpath=(r'C:\Users\A B\src "quoted"',),
            dotenv=(r'C:\Users\A B\.env "quoted"',),
            sandbox_context={},
            dependency_context={},
        )
        header = build_worker_notebook(runtime)
        parsed = _script_metadata.loads(header)
        self.assertEqual(
            parsed["tool"]["marimo"]["runtime"]["pythonpath"],
            [r'C:\Users\A B\src "quoted"'],
        )
        self.assertEqual(
            parsed["tool"]["marimo"]["runtime"]["dotenv"],
            [r'C:\Users\A B\.env "quoted"'],
        )

    def test_parent_export_code_uses_only_validated_names_and_unique_helpers(self):
        code_a = build_parent_export_code(
            ["records"], Path("C:/safe/batch"), Path("C:/safe/meta"),
            Path("C:/safe/helper.py"), Path("C:/safe/parent.py"),
        )
        code_b = build_parent_export_code(
            ["records"], Path("C:/safe/batch"), Path("C:/safe/meta"),
            Path("C:/safe/helper.py"), Path("C:/safe/parent.py"),
        )
        self.assertNotEqual(code_a, code_b)
        compile(code_a, "<parent-export>", "exec")
        self.assertIn("records", code_a)
        self.assertNotIn("globals().update", code_a)
        with self.assertRaises(ValueError):
            build_parent_export_code(
                ["_private"], Path("batch"), Path("meta"), Path("helper"), Path("parent"),
            )


if __name__ == "__main__":
    unittest.main()
