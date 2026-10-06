"""Named-value snapshots and runtime metadata for marimo-pair workers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import keyword
import os
import pickle
import platform
import socket
import sys
import threading
import types
import uuid
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


class BranchStateError(ValueError):
    """A named value or parent runtime could not be transferred safely."""

    def __init__(
        self,
        name: str,
        value_type: str,
        operation: str,
        detail: object,
    ) -> None:
        self.name = name
        self.value_type = value_type
        self.operation = operation
        super().__init__(
            f"Cannot {operation} variable {name!r} ({value_type}): {detail}"
        )


@dataclass(frozen=True)
class ParentRuntime:
    notebook_path: str
    config_sources: dict[str, str]
    cwd: str
    sys_path: tuple[str, ...]
    python_executable: str
    python_version: str
    pythonpath: tuple[str, ...]
    dotenv: tuple[str, ...]
    sandbox_context: dict[str, Any]
    dependency_context: dict[str, Any]


def validate_names(
    names: Sequence[str], existing: Collection[str] = ()
) -> list[str]:
    """Validate marimo-visible Python identifiers and preserve request order."""
    result = list(names)
    seen: set[str] = set()
    for name in result:
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            raise ValueError(f"Invalid Python identifier: {name!r}")
        if name.startswith("_"):
            raise ValueError(f"Private marimo cell names cannot be transferred: {name!r}")
        if name in existing or name in seen:
            raise ValueError(f"Variable name already exists or was repeated: {name!r}")
        seen.add(name)
    return result


def _type_name(value: object) -> str:
    cls = value if isinstance(value, type) else type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _module_name(value: object) -> str:
    if isinstance(value, (types.FunctionType, types.BuiltinFunctionType, type)):
        return str(getattr(value, "__module__", ""))
    return type(value).__module__


def _unsupported_kind(value: object) -> str | None:
    module = _module_name(value)
    name = getattr(value, "__name__", type(value).__name__)
    if module == "__main__":
        return "notebook __main__ definitions cannot be restored in another kernel"
    if module.startswith("marimo._plugins.ui"):
        return "marimo UI values must be exported as their underlying plain value"
    if module.startswith("marimo._runtime.state") or (
        module.startswith("marimo._runtime")
        and name in {"State", "RuntimeContext", "KernelRuntimeContext"}
    ):
        return "marimo state and runtime-context values cannot be transferred"
    if isinstance(value, types.ModuleType):
        return "module objects are process-wide resources"
    if isinstance(value, io.IOBase):
        return "open file resources cannot be transferred"
    if isinstance(value, socket.socket):
        return "socket resources cannot be transferred"
    if isinstance(value, threading.Thread):
        return "active threads cannot be transferred"
    if isinstance(value, concurrent.futures.Future):
        return "active futures cannot be transferred"
    if isinstance(value, asyncio.Future):
        return "active asyncio tasks/futures cannot be transferred"
    if isinstance(value, (types.GeneratorType, types.CoroutineType, types.AsyncGeneratorType)):
        return "active generators and coroutines cannot be transferred"
    if isinstance(value, memoryview):
        return "memory views over external buffers cannot be transferred"
    lock_type = type(threading.Lock())
    if isinstance(value, lock_type):
        return "locks cannot be transferred"
    return None


def _check_value_graph(name: str, value: object) -> None:
    """Reject unsupported objects anywhere in a value's reachable state."""
    seen: set[int] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        reason = _unsupported_kind(current)
        if reason:
            raise BranchStateError(name, _type_name(current), "export", reason)

        if isinstance(current, Mapping):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set, frozenset, deque)):
            pending.extend(current)

        if isinstance(current, types.FunctionType):
            pending.extend(current.__defaults__ or ())
            pending.extend((current.__kwdefaults__ or {}).values())
            if current.__closure__:
                for cell in current.__closure__:
                    try:
                        pending.append(cell.cell_contents)
                    except ValueError:
                        pass
            continue

        if isinstance(current, (str, bytes, bytearray, int, float, complex, bool, type(None), type, Path)):
            continue
        try:
            attributes = vars(current)
        except TypeError:
            attributes = {}
        pending.extend(attributes.values())
        for cls in type(current).__mro__:
            slots = cls.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot in {"__dict__", "__weakref__"}:
                    continue
                descriptor = cls.__dict__.get(slot)
                if isinstance(descriptor, types.MemberDescriptorType):
                    try:
                        pending.append(descriptor.__get__(current, type(current)))
                    except AttributeError:
                        pass


def _open_private(path: Path, mode: str, *, encoding: str | None = None):
    if not path.is_absolute():
        raise ValueError(f"Artifact path must be absolute: {path}")
    if not path.parent.is_dir():
        raise ValueError(f"Artifact directory does not exist: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, mode, encoding=encoding) if encoding else os.fdopen(fd, mode)


def write_batch(values: Mapping[str, object], path: Path) -> None:
    """Serialize one shared-memo record stream, deleting partial output on error."""
    names = validate_names(list(values))
    created = False
    complete = False
    current_name = "<batch>"
    current_value: object = values
    try:
        stream = _open_private(Path(path), "wb")
        created = True
        with stream:
            pickler = pickle.Pickler(stream, protocol=pickle.HIGHEST_PROTOCOL)
            for current_name in names:
                current_value = values[current_name]
                _check_value_graph(current_name, current_value)
                type_name = _type_name(current_value)
                try:
                    pickler.dump(current_name)
                    pickler.dump(type_name)
                    pickler.dump(current_value)
                except Exception as error:
                    raise BranchStateError(
                        current_name, type_name, "export", error
                    ) from error
        complete = True
    except BranchStateError:
        raise
    except Exception as error:
        if current_name != "<batch>":
            raise BranchStateError(
                current_name, _type_name(current_value), "export", error
            ) from error
        raise
    finally:
        if created and not complete:
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass


def read_batch(path: Path) -> dict[str, object]:
    """Restore a shared-memo stream, identifying the record that failed."""
    values: dict[str, object] = {}
    current_name = "<batch>"
    type_name = "unknown"
    try:
        with Path(path).open("rb") as stream:
            unpickler = pickle.Unpickler(stream)
            while True:
                try:
                    current_name = unpickler.load()
                except EOFError:
                    return values
                try:
                    type_name = unpickler.load()
                    value = unpickler.load()
                except Exception as error:
                    raise BranchStateError(
                        str(current_name), str(type_name), "restore", error
                    ) from error
                if not isinstance(current_name, str) or current_name in values:
                    raise BranchStateError(
                        str(current_name), str(type_name), "restore", "invalid batch record"
                    )
                validate_names([current_name], values.keys())
                values[current_name] = value
    except BranchStateError:
        raise
    except Exception as error:
        raise BranchStateError(
            str(current_name), str(type_name), "restore", error
        ) from error


def _is_absolute_path(value: str | os.PathLike[str]) -> bool:
    text = os.fspath(value)
    return Path(text).is_absolute() or PureWindowsPath(text).is_absolute()


def _absolute_path(value: str | os.PathLike[str], base: Path | None = None) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return str(path.absolute())


def _path_key(value: str, cwd: str) -> str:
    path = value or cwd
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _check_import_path(path: str, name: str) -> None:
    candidate = Path(path)
    mode = os.R_OK | (os.X_OK if candidate.is_dir() else 0)
    if not candidate.exists() or not os.access(candidate, mode):
        raise BranchStateError(name, "path", "restore", f"required path is inaccessible: {path}")


def _check_dotenv(path: str) -> None:
    candidate = Path(path)
    if not candidate.is_file() or not os.access(candidate, os.R_OK):
        raise BranchStateError(
            "<runtime.dotenv>", "path", "capture", f"required dotenv file is inaccessible: {path}"
        )


def _load_parent_config(notebook: Path) -> tuple[object, Mapping[str, Any]]:
    from marimo._config.manager import get_default_config_manager

    manager = get_default_config_manager(current_path=str(notebook))
    return manager, manager.get_config(hide_secrets=False)


def _config_sources(manager: object, notebook: Path) -> dict[str, str]:
    sources = {"notebook": str(notebook)}
    for partial in getattr(manager, "partials", ()):
        project_path = getattr(partial, "pyproject_path", None)
        if project_path is not None:
            sources["project"] = str(Path(project_path).absolute())
        filename = getattr(partial, "filename", None)
        if filename:
            sources["script"] = str(Path(filename).absolute())
    user_manager = getattr(manager, "user_config_mgr", None)
    get_config_path = getattr(user_manager, "get_config_path", None)
    if callable(get_config_path):
        try:
            user_path = get_config_path()
        except Exception:
            user_path = None
        if user_path:
            sources["user"] = str(Path(user_path).absolute())
    return sources


def _script_runtime(notebook: Path) -> dict[str, Any]:
    try:
        from marimo._environments import script_metadata

        parsed = script_metadata.loads(notebook.read_text(encoding="utf-8")) or {}
    except Exception as error:
        raise BranchStateError(
            "<runtime>", "script metadata", "capture", error
        ) from error
    return (
        parsed.get("tool", {})
        .get("marimo", {})
        .get("runtime", {})
    )


def _runtime_paths(
    manager_config: Mapping[str, Any], notebook: Path, script_runtime: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    runtime = manager_config.get("runtime", {})
    if not isinstance(runtime, Mapping):
        raise BranchStateError("<runtime>", _type_name(runtime), "capture", "runtime config is invalid")

    # The merged project pythonpath has already been resolved by marimo against
    # pyproject.toml; script-relative settings are instead notebook-relative.
    if "pythonpath" in script_runtime:
        pythonpath_values = script_runtime["pythonpath"]
    else:
        pythonpath_values = runtime.get("pythonpath", [])
    if not isinstance(pythonpath_values, (list, tuple)):
        raise BranchStateError(
            "<runtime.pythonpath>", _type_name(pythonpath_values), "capture", "expected a path list"
        )
    pythonpath = tuple(_absolute_path(str(path), notebook.parent) for path in pythonpath_values)

    if "dotenv" in script_runtime:
        dotenv_values = script_runtime["dotenv"]
        dotenv_base = notebook.parent
    else:
        dotenv_values = runtime.get("dotenv", [])
        dotenv_base = None
    if not isinstance(dotenv_values, (list, tuple)):
        raise BranchStateError(
            "<runtime.dotenv>", _type_name(dotenv_values), "capture", "expected a path list"
        )
    dotenv = tuple(_absolute_path(str(path), dotenv_base) for path in dotenv_values)
    return pythonpath, dotenv


def _capture_parent_runtime(notebook_path: Path) -> dict[str, Any]:
    notebook = notebook_path.expanduser().absolute()
    if not notebook.is_file() or not os.access(notebook, os.R_OK):
        raise BranchStateError(
            "<runtime>", "notebook path", "capture", f"notebook is inaccessible: {notebook}"
        )
    try:
        manager, config = _load_parent_config(notebook)
        script_runtime = _script_runtime(notebook)
        pythonpath, dotenv = _runtime_paths(config, notebook, script_runtime)
    except BranchStateError:
        raise
    except Exception as error:
        raise BranchStateError(
            "<runtime>", "marimo config", "capture", error
        ) from error

    live_paths = {_path_key(entry, os.getcwd()) for entry in sys.path}
    for path in pythonpath:
        _check_import_path(path, "<runtime.pythonpath>")
        if _path_key(path, os.getcwd()) not in live_paths:
            raise BranchStateError(
                "<runtime.pythonpath>", "path", "capture",
                f"configured import root is not active in parent sys.path: {path}",
            )
    for path in dotenv:
        _check_dotenv(path)

    config_sources = _config_sources(manager, notebook)
    project_path = Path(config_sources["project"]) if "project" in config_sources else None
    # Keep the context bounded to paths and interpreter identity; never copy
    # arbitrary environment variables (which may contain credentials).
    sandbox_context = {
        "virtual_env": os.environ.get("VIRTUAL_ENV"),
        "conda_prefix": os.environ.get("CONDA_PREFIX"),
        "is_virtualenv": sys.prefix != sys.base_prefix,
    }
    dependency_context = {
        "implementation": platform.python_implementation(),
        "python_prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "project_root": str(project_path.parent) if project_path is not None else None,
    }
    return {
        "notebook_path": str(notebook),
        "config_sources": config_sources,
        "cwd": os.getcwd(),
        "sys_path": list(sys.path),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "pythonpath": list(pythonpath),
        "dotenv": list(dotenv),
        "sandbox_context": sandbox_context,
        "dependency_context": dependency_context,
    }


def _write_metadata(data: Mapping[str, Any], path: Path) -> None:
    stream = _open_private(path, "w", encoding="utf-8")
    try:
        with stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def export_named_values(
    scope: Mapping[str, object],
    names: Sequence[str],
    bundle_path: Path,
    metadata_path: Path,
    notebook_path: Path,
) -> None:
    """Write selected values and reproducible, secret-free parent context."""
    selected = validate_names(names)
    if not isinstance(scope, Mapping):
        raise TypeError("scope must be a name-to-value mapping")
    missing = [name for name in selected if name not in scope]
    if missing:
        raise BranchStateError(missing[0], "missing", "export", "name is not present in parent scope")
    bundle = Path(bundle_path)
    metadata = Path(metadata_path)
    if bundle.absolute() == metadata.absolute():
        raise ValueError("bundle and metadata paths must differ")
    for label, artifact in (("bundle", bundle), ("metadata", metadata)):
        if not artifact.is_absolute() or not artifact.parent.is_dir():
            raise BranchStateError(
                f"<artifact.{label}>",
                "path",
                "export",
                "artifact path must be absolute with an existing directory",
            )
    runtime = _capture_parent_runtime(Path(notebook_path))
    bundle_created = False
    metadata_created = False
    try:
        write_batch({name: scope[name] for name in selected}, bundle)
        bundle_created = True
        _write_metadata(runtime, metadata)
        metadata_created = True
    except Exception:
        if bundle_created:
            try:
                bundle.unlink()
            except FileNotFoundError:
                pass
        if metadata_created:
            try:
                metadata.unlink()
            except FileNotFoundError:
                pass
        raise


def read_parent_runtime(metadata_path: Path) -> ParentRuntime:
    """Load captured runtime context and reject paths this host cannot reproduce."""
    metadata = Path(metadata_path)
    if not metadata.is_absolute():
        raise BranchStateError("<runtime.metadata>", "path", "restore", "metadata path must be absolute")
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
        notebook = str(data["notebook_path"])
        cwd = str(data["cwd"])
        parent_sys_path = tuple(str(path) for path in data["sys_path"])
        python_executable = str(data["python_executable"])
        python_version = str(data["python_version"])
        pythonpath = tuple(str(path) for path in data["pythonpath"])
        dotenv = tuple(str(path) for path in data["dotenv"])
        config_sources = {str(k): str(v) for k, v in data["config_sources"].items()}
        sandbox_context = dict(data["sandbox_context"])
        dependency_context = dict(data["dependency_context"])
    except Exception as error:
        raise BranchStateError(
            "<runtime>", "metadata", "restore", f"invalid runtime metadata: {error}"
        ) from error

    if not Path(cwd).is_absolute() or not Path(cwd).is_dir() or not os.access(cwd, os.R_OK | os.X_OK):
        raise BranchStateError("<runtime.cwd>", "path", "restore", f"parent cwd is inaccessible: {cwd}")
    if not Path(notebook).is_absolute() or not Path(notebook).is_file() or not os.access(notebook, os.R_OK):
        raise BranchStateError("<runtime.notebook>", "path", "restore", f"parent notebook is inaccessible: {notebook}")
    current_executable = Path(sys.executable)
    captured_executable = Path(python_executable)
    if not current_executable.is_file() or not captured_executable.is_file():
        raise BranchStateError(
            "<runtime.python>", "executable", "restore", "parent Python executable is inaccessible"
        )
    try:
        same_executable = os.path.samefile(current_executable, captured_executable)
    except OSError:
        same_executable = os.path.normcase(str(current_executable)) == os.path.normcase(str(captured_executable))
    if not same_executable or platform.python_version() != python_version:
        raise BranchStateError(
            "<runtime.python>", "executable", "restore",
            "parent Python executable/version cannot be reproduced by this controller",
        )
    if dependency_context.get("implementation") != platform.python_implementation():
        raise BranchStateError(
            "<runtime.python>", "interpreter", "restore", "Python implementation differs from parent"
        )
    if dependency_context.get("python_prefix") != sys.prefix or dependency_context.get("base_prefix") != sys.base_prefix:
        raise BranchStateError(
            "<runtime.dependencies>", "environment", "restore",
            "parent Python dependency environment cannot be reproduced",
        )
    if (
        sandbox_context.get("virtual_env") != os.environ.get("VIRTUAL_ENV")
        or sandbox_context.get("conda_prefix") != os.environ.get("CONDA_PREFIX")
        or sandbox_context.get("is_virtualenv") != (sys.prefix != sys.base_prefix)
    ):
        raise BranchStateError(
            "<runtime.sandbox>", "environment", "restore",
            "parent sandbox/dependency context cannot be reproduced",
        )

    for path in pythonpath:
        if not _is_absolute_path(path):
            raise BranchStateError("<runtime.pythonpath>", "path", "restore", f"path is not absolute: {path}")
        _check_import_path(path, "<runtime.pythonpath>")
    for path in dotenv:
        if not _is_absolute_path(path):
            raise BranchStateError("<runtime.dotenv>", "path", "restore", f"path is not absolute: {path}")
        _check_dotenv(path)

    # Add roots present only in the parent kernel. The worker uses this field
    # for its header; the bootstrap later restores the exact captured sys.path.
    controller_paths = {_path_key(entry, cwd) for entry in sys.path}
    configured_paths = {_path_key(entry, cwd) for entry in pythonpath}
    additional: list[str] = []
    for entry in parent_sys_path:
        if not entry:
            candidate = cwd
        else:
            candidate = _absolute_path(entry, Path(cwd))
        key = _path_key(candidate, cwd)
        if key in controller_paths or key in configured_paths:
            continue
        _check_import_path(candidate, "<runtime.sys_path>")
        additional.append(candidate)
        configured_paths.add(key)
    effective_pythonpath = tuple(dict.fromkeys((*pythonpath, *additional)))
    return ParentRuntime(
        notebook_path=notebook,
        config_sources=config_sources,
        cwd=cwd,
        sys_path=parent_sys_path,
        python_executable=python_executable,
        python_version=python_version,
        pythonpath=effective_pythonpath,
        dotenv=dotenv,
        sandbox_context=sandbox_context,
        dependency_context=dependency_context,
    )


def _literal(value: str) -> str:
    return repr(value)


def _toml_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _batch_id() -> str:
    return uuid.uuid4().hex


def build_parent_export_code(
    names: Sequence[str],
    bundle_path: Path,
    metadata_path: Path,
    helper_path: Path,
    notebook_path: Path,
) -> str:
    """Generate a fixed parent scratchpad program accepting names, not code."""
    checked = validate_names(names)
    for label, path in (
        ("bundle", bundle_path),
        ("metadata", metadata_path),
        ("helper", helper_path),
        ("notebook", notebook_path),
    ):
        if not _is_absolute_path(path):
            raise ValueError(f"{label} path must be absolute: {path}")
    batch_id = _batch_id()
    prefix = f"_marimo_pair_{batch_id}"
    names_literal = repr(tuple(checked))
    return f'''import importlib.util as {prefix}_importlib_util
import pathlib as {prefix}_pathlib
import sys as {prefix}_sys
{prefix}_helper_name = {_literal(prefix + '_helper')}
{prefix}_dont_write = {prefix}_sys.dont_write_bytecode
{prefix}_sys.dont_write_bytecode = True
try:
    {prefix}_spec = {prefix}_importlib_util.spec_from_file_location({prefix}_helper_name, {_literal(str(helper_path))})
    {prefix}_helper = {prefix}_importlib_util.module_from_spec({prefix}_spec)
    {prefix}_spec.loader.exec_module({prefix}_helper)
finally:
    {prefix}_sys.dont_write_bytecode = {prefix}_dont_write
{prefix}_helper.export_named_values(
    globals(),
    {names_literal},
    {prefix}_pathlib.Path({_literal(str(bundle_path))}),
    {prefix}_pathlib.Path({_literal(str(metadata_path))}),
    {prefix}_pathlib.Path({_literal(str(notebook_path))}),
)
'''


def _validate_header_paths(paths: Sequence[str], field: str) -> list[str]:
    result = list(paths)
    for path in result:
        if not _is_absolute_path(path):
            raise ValueError(f"{field} must contain absolute paths: {path!r}")
    return result


def build_worker_notebook(runtime: ParentRuntime) -> str:
    """Build a minimal worker notebook with TOML-safe effective path settings."""
    pythonpath = _validate_header_paths(runtime.pythonpath, "runtime.pythonpath")
    dotenv = _validate_header_paths(runtime.dotenv, "runtime.dotenv")
    config = {
        "tool": {
            "marimo": {
                "runtime": {
                    "pythonpath": pythonpath,
                    "dotenv": dotenv,
                }
            }
        }
    }
    # JSON string escaping is a strict subset of TOML basic-string escaping.
    lines = ["# /// script", "# [tool.marimo.runtime]"]
    for key in ("pythonpath", "dotenv"):
        values = config["tool"]["marimo"]["runtime"][key]
        lines.append(f"# {key} = [{', '.join(_toml_literal(path) for path in values)}]")
    lines.extend(["# ///", "import marimo", "", "app = marimo.App()", ""])
    return "\n".join(lines)


def build_worker_bootstrap_cell(
    names: Sequence[str],
    bundle_path: Path,
    metadata_path: Path,
    helper_path: Path,
) -> str:
    """Generate worker-local restore code with explicit public assignments."""
    checked = validate_names(names)
    for label, path in (("bundle", bundle_path), ("metadata", metadata_path), ("helper", helper_path)):
        if not _is_absolute_path(path):
            raise ValueError(f"{label} path must be absolute: {path}")
    batch_id = _batch_id()
    prefix = f"_marimo_pair_{batch_id}"
    assignments = "\n".join(
        f"{name} = {prefix}_values[{name!r}]" for name in checked
    )
    return f'''import importlib.util as {prefix}_importlib_util
import os as {prefix}_os
import pathlib as {prefix}_pathlib
import sys as {prefix}_sys
{prefix}_dont_write = {prefix}_sys.dont_write_bytecode
{prefix}_sys.dont_write_bytecode = True
try:
    {prefix}_helper_name = {_literal(prefix + '_helper')}
    {prefix}_spec = {prefix}_importlib_util.spec_from_file_location({prefix}_helper_name, {_literal(str(helper_path))})
    {prefix}_helper = {prefix}_importlib_util.module_from_spec({prefix}_spec)
    {prefix}_spec.loader.exec_module({prefix}_helper)
finally:
    {prefix}_sys.dont_write_bytecode = {prefix}_dont_write
{prefix}_runtime = {prefix}_helper.read_parent_runtime({prefix}_pathlib.Path({_literal(str(metadata_path))}))
{prefix}_os.chdir({prefix}_runtime.cwd)
{prefix}_sys.path[:] = list({prefix}_runtime.sys_path)
{prefix}_values = {prefix}_helper.read_batch({prefix}_pathlib.Path({_literal(str(bundle_path))}))
{assignments}
'''
