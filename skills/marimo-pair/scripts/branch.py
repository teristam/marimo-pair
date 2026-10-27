"""Authenticated worker lifecycle and coordinator-only branch commands."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

# The coordinator must not emit repository bytecode artifacts while importing
# its fixed helper or starting workers.
sys.dont_write_bytecode = True

import branch_state
from branch_state import (
    ParentRuntime,
    build_parent_export_code,
    build_worker_bootstrap_cell,
    build_worker_notebook,
    read_parent_runtime,
    validate_names,
)


class BranchError(RuntimeError):
    """A parent or worker lifecycle operation failed safely."""


# Upper bound for one `start --count`; guards against typos spawning many servers.
MAX_WORKERS_PER_START = 16


@dataclass(frozen=True)
class ParentTarget:
    url: str
    session_id: str
    notebook_path: Path


@dataclass
class WorkerRecord:
    worker_id: str
    url: str
    session_id: str
    token_path: Path
    notebook_path: Path
    temp_dir: Path
    runtime_path: Path
    parent_url: str
    parent_notebook_path: Path
    parent_session_id: str
    status: str
    server_pid: int | None
    process_identity: str | None
    imported_names: tuple[str, ...]
    log_path: Path
    bundle_path: Path
    helper_path: Path
    drainer_pid: int | None = None
    drainer_identity: str | None = None
    starter_pid: int | None = None
    starter_identity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in (
            "token_path", "notebook_path", "temp_dir", "runtime_path",
            "parent_notebook_path", "log_path", "bundle_path", "helper_path",
        ):
            result[key] = str(result[key])
        result["imported_names"] = list(self.imported_names)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerRecord":
        copied = dict(data)
        for key in (
            "token_path", "notebook_path", "temp_dir", "runtime_path",
            "parent_notebook_path", "log_path", "bundle_path", "helper_path",
        ):
            copied[key] = Path(copied[key])
        copied["imported_names"] = tuple(copied.get("imported_names", ()))
        copied.setdefault("starter_pid", None)
        copied.setdefault("starter_identity", None)
        return cls(**copied)


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    output: str
    stderr: str = ""
    returncode: int = 0


@dataclass(frozen=True)
class SSEEvent:
    event: str | None
    data: Any


def _normalize_marker(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def parse_sse_records(lines: Iterable[str | bytes]) -> Iterator[SSEEvent]:
    """Parse complete SSE records and decode each data payload as JSON."""
    event: str | None = None
    data_lines: list[str] = []

    def finish() -> SSEEvent | None:
        nonlocal event, data_lines
        if not data_lines and event is None:
            return None
        payload = "\n".join(data_lines)
        current_event = event
        event = None
        data_lines = []
        if not payload.strip():
            return SSEEvent(current_event, None)
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError) as error:
            raise BranchError(f"SSE record was not valid JSON: {error}") from error
        return SSEEvent(current_event, decoded)

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.rstrip("\r\n")
        if not line:
            complete = finish()
            if complete is not None:
                yield complete
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value.strip()
        elif field == "data":
            data_lines.append(value)
    complete = finish()
    if complete is not None:
        yield complete


def _event_text(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _event_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _event_text(item)
    elif isinstance(value, str):
        yield value


def wait_for_kernel_ready(stream: Iterable[str | bytes], timeout: float = 30.0) -> SSEEvent:
    """Drain SSE through kernel-ready; fail closed on startup errors/EOF."""
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        for item in parse_sse_records(stream):
            if time.monotonic() > deadline:
                raise BranchError("Timed out waiting for worker kernel-ready")
            normalized_event = _normalize_marker(item.event or "")
            text = " ".join(_event_text(item.data))
            normalized_text = _normalize_marker(text)
            if normalized_event == "close":
                raise BranchError(f"Worker SSE closed before readiness: {text[:300]}")
            if "kernelstartuperror" in normalized_text or "kernelstartuperror" in normalized_event:
                raise BranchError(f"Worker kernel startup failed: {text[:500]}")
            if "kernelready" in normalized_text or normalized_event == "kernelready":
                return item
    except BranchError:
        raise
    except (TimeoutError, OSError, socket.timeout) as error:
        raise BranchError(f"Timed out waiting for worker kernel-ready: {error}") from error
    raise BranchError("Worker SSE ended before kernel-ready")


def redact_log_text(text: str, tokens: Sequence[str]) -> str:
    """Redact exact credentials and every URL access_token parameter."""
    for token in tokens:
        if token:
            text = text.replace(token, "[REDACTED]")
    return re.sub(
        r"(?i)(access_token=)[^&\s\"'<>]*",
        r"\1[REDACTED]",
        text,
    )


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


def _per_user_root() -> Path:
    override = os.environ.get("MARIMO_PAIR_HOME")
    if override:
        candidate = Path(override).expanduser().absolute()
        temp_root = Path(tempfile.gettempdir()).resolve()
        try:
            candidate.resolve().relative_to(temp_root)
        except (OSError, ValueError) as error:
            raise BranchError("MARIMO_PAIR_HOME must remain inside the system temporary directory") from error
        return candidate
    if os.name == "nt":
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    else:
        try:
            user = str(os.getuid())
        except AttributeError:
            user = os.environ.get("USER", "user")
    suffix = hashlib.sha256(user.encode("utf-8", errors="replace")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"marimo-pair-{suffix}"


def _private_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
            if path.stat().st_mode & 0o077:
                raise OSError("directory permissions are not private")
        except OSError as error:
            raise BranchError(f"Could not secure private worker directory {path}: {error}") from error


def _write_private(path: Path, data: str | bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8"}
    try:
        with os.fdopen(fd, mode, **kwargs) as stream:
            stream.write(data)
        if os.name != "nt":
            path.chmod(0o600)
            if path.stat().st_mode & 0o077:
                raise OSError("file permissions are not private")
    except OSError as error:
        try:
            path.unlink()
        except OSError:
            pass
        raise BranchError(f"Could not secure private worker file {path}: {error}") from error


def _path_text(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def _real_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and not PureWindowsPath(str(path)).is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise BranchError(f"Required local path is inaccessible: {path}: {error}") from error


def _same_notebook_path(first: str | Path, second: str | Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        try:
            a = os.path.normcase(os.path.realpath(os.path.abspath(first)))
            b = os.path.normcase(os.path.realpath(os.path.abspath(second)))
            return a == b
        except (OSError, TypeError):
            return False


def _session_path(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    for key in ("path", "filename", "notebook_path"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _process_executable(pid: int) -> str:
    if sys.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return result.stdout.strip()
    return ""


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


def _windows_kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.GetProcessTimes.restype = ctypes.c_int
    kernel.GetProcessTimes.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
    ]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    return kernel


def process_identity(pid: int) -> str | None:
    """Return process creation identity without a PID-alive kill(0) probe."""
    if pid < 1:
        return None
    if os.name == "nt":
        try:
            kernel = _windows_kernel32()
            access = 0x00100000 | 0x1000  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
            handle = kernel.OpenProcess(access, False, pid)
            if not handle:
                return None
            try:
                # OpenProcess and GetProcessTimes can still succeed for an exited
                # process while a Popen/other handle keeps its kernel object alive.
                # A signaled handle is definitively gone, not a live PID identity.
                wait_result = kernel.WaitForSingleObject(handle, 0)
                if wait_result != 0x102:  # Only WAIT_TIMEOUT proves this process is still live.
                    return None
                creation, exit_time, kernel_time, user_time = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
                if not kernel.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_time),
                    ctypes.byref(kernel_time), ctypes.byref(user_time),
                ):
                    return None
                value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                return f"win:{value}"
            finally:
                kernel.CloseHandle(handle)
        except (AttributeError, OSError):
            return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            tail = stat[stat.rfind(")") + 2 :].split()
            if tail[0] in {"Z", "X"}:
                return None
            start_ticks = tail[19]
            return f"linux:{start_ticks}:{_process_executable(pid)}"
        except (OSError, IndexError, ValueError):
            return None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart=,comm=,stat="],
                capture_output=True, text=True, timeout=2, check=False,
            )
            fields = result.stdout.strip().split()
            # BSD ps lstart is five whitespace-separated fields. The trailing
            # state is used only for liveness; it changes (S/I/R/...) over time
            # and must not be part of the process-creation identity.
            if result.returncode == 0 and len(fields) >= 7:
                state = fields[-1]
                if state.startswith(("Z", "X")):
                    return None
                start_time = " ".join(fields[:5])
                executable = " ".join(fields[5:-1])
                if start_time and executable:
                    return f"mac:{start_time}:{executable}"
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=,comm="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return f"other:{result.stdout.strip()}" if result.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def process_matches(pid: int | None, identity: str | None) -> bool:
    return bool(pid and identity and process_identity(pid) == identity)


def _pid_alive_state(pid: int) -> bool | None:
    """Return alive/dead, or None when platform APIs cannot prove either."""
    if pid < 1:
        return False
    if os.name == "nt":
        try:
            kernel = _windows_kernel32()
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if handle:
                try:
                    result = kernel.WaitForSingleObject(handle, 0)
                    if result == 0:  # WAIT_OBJECT_0: process has exited
                        return False
                    if result == 0x102:  # WAIT_TIMEOUT: process is still running
                        return True
                    return None
                finally:
                    kernel.CloseHandle(handle)
            error = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            return False if error == 87 else None
        except (AttributeError, OSError):
            return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            tail = stat[stat.rfind(")") + 2 :].split()
            return tail[0] not in {"Z", "X"}
        except FileNotFoundError:
            return False
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,stat="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            state = result.stdout.strip().split()[-1]
            return not state.startswith(("Z", "X"))
        if result.returncode == 1:
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _original_process_gone(pid: int | None, identity: str | None) -> bool:
    if pid is None:
        return True
    current = process_identity(pid)
    if current is not None:
        return identity is not None and current != identity
    return _pid_alive_state(pid) is False


def wait_for_process(pid: int, identity: str | None, timeout: float) -> bool:
    """Wait by platform process handles/creation identity, never Windows kill(0)."""
    deadline = time.monotonic() + max(0, timeout)
    if os.name == "nt":
        try:
            kernel = _windows_kernel32()
            handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
            if not handle:
                return _pid_alive_state(pid) is False
            try:
                # A signaled handle proves this process has exited, even when
                # process_identity() can no longer return a live identity.
                if kernel.WaitForSingleObject(handle, 0) == 0:
                    return True
                current = process_identity(pid)
                if identity is None or current is None:
                    return False
                if current != identity:
                    return True
                while True:
                    remaining = deadline - time.monotonic()
                    milliseconds = max(0, min(250, int(remaining * 1000)))
                    result = kernel.WaitForSingleObject(handle, milliseconds)
                    if result == 0:
                        return True
                    if result != 0x102:
                        return _original_process_gone(pid, identity)
                    if time.monotonic() >= deadline:
                        return False
            finally:
                kernel.CloseHandle(handle)
        except (AttributeError, OSError):
            return _original_process_gone(pid, identity)
    while True:
        current = process_identity(pid)
        if current is not None and identity is not None and current != identity:
            return True
        if current is None:
            return _pid_alive_state(pid) is False
        if identity is None:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))


def _path_bash_candidates() -> list[Path]:
    candidates: list[Path] = []
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir:
            continue
        candidate = Path(raw_dir) / "bash.exe"
        if candidate.is_file():
            candidates.append(candidate)
    return candidates


def _is_wsl_launcher(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    segments = normalized.split("/")
    if "system32" in segments or "sysnative" in segments:
        return True
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        try:
            return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(Path(local) / "Microsoft" / "WindowsApps")))) == os.path.normcase(str(Path(local) / "Microsoft" / "WindowsApps"))
        except (ValueError, OSError):
            pass
    return "/microsoft/windowsapps/" in normalized


def _git_bash_candidate() -> Path | None:
    git = shutil.which("git")
    if not git:
        return None
    executable = Path(git).resolve()
    # Standard Git for Windows layouts are <install>/cmd/git.exe or <install>/bin/git.exe.
    root = executable.parent.parent if executable.parent.name.lower() in {"cmd", "bin"} else executable.parent
    candidate = root / "bin" / "bash.exe"
    return candidate if candidate.is_file() else None


def _bash_environment() -> dict[str, str]:
    env = _without_token_sources(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _bash_ostype(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "-c", "printf '%s' \"${OSTYPE:-}\""],
            capture_output=True, text=True, timeout=5, check=False,
            env=_bash_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BranchError(f"Could not validate Bash executable {path}: {error}") from error
    if result.returncode != 0:
        raise BranchError(f"Bash executable did not run successfully: {path}")
    return result.stdout.strip()


def resolve_bash() -> Path:
    """Select only a usable Bash; Windows candidates must be MSYS/Cygwin."""
    override = os.environ.get("MARIMO_PAIR_BASH")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise BranchError(f"MARIMO_PAIR_BASH is not an executable file: {candidate}")
        if os.name == "nt":
            ostype = _bash_ostype(candidate).lower()
            if not ostype.startswith(("msys", "cygwin")):
                raise BranchError("MARIMO_PAIR_BASH must report an MSYS or Cygwin $OSTYPE")
        elif not os.access(candidate, os.X_OK):
            raise BranchError(f"MARIMO_PAIR_BASH is not executable: {candidate}")
        return candidate
    if os.name != "nt":
        found = shutil.which("bash")
        if not found:
            raise BranchError("Bash is required to run execute-code.sh")
        candidate = Path(found)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise BranchError(f"Bash lookup did not return an executable file: {candidate}")
        return candidate
    for candidate in _path_bash_candidates():
        if _is_wsl_launcher(candidate):
            continue
        try:
            ostype = _bash_ostype(candidate).lower()
        except BranchError:
            continue
        if ostype.startswith(("msys", "cygwin")):
            return candidate
    fallback = _git_bash_candidate()
    if fallback is not None:
        try:
            ostype = _bash_ostype(fallback).lower()
        except BranchError:
            ostype = ""
        if ostype.startswith(("msys", "cygwin")):
            return fallback
    raise BranchError("No Git Bash or Cygwin Bash was found; set MARIMO_PAIR_BASH to a valid executable")


def _bash_run(bash: Path, command: str, timeout: float = 10) -> str:
    try:
        result = subprocess.run(
            [str(bash), "-lc", command], capture_output=True, text=True,
            timeout=timeout, check=False,
            env=_bash_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BranchError(f"Could not run Bash preflight: {error}") from error
    if result.returncode:
        output = (result.stderr or result.stdout).strip()
        raise BranchError(f"Bash preflight failed: {output}")
    return result.stdout


def preflight_bash(bash: Path) -> None:
    _bash_run(bash, "command -v jq >/dev/null && command -v curl >/dev/null")


class WorkerManager:
    """Manage independently authenticated marimo edit-mode worker servers."""

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        startup_timeout: float = 45.0,
        request_timeout: float = 10.0,
        execution_timeout: float | None = 300.0,
        shutdown_timeout: float = 10.0,
        parent_token_file: str | None = None,
    ) -> None:
        self.root_dir = Path(root_dir or _per_user_root()).expanduser().absolute()
        self.registry_dir = self.root_dir / "workers"
        self.workers_dir = self.registry_dir  # compatibility name: per-worker registry files
        self.temp_root = self.root_dir / "temp"
        _private_mkdir(self.root_dir)
        _private_mkdir(self.registry_dir)
        _private_mkdir(self.temp_root)
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.execution_timeout = execution_timeout
        self.shutdown_timeout = shutdown_timeout
        self.parent_token_file = parent_token_file
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._drainers: dict[str, tuple[Any, threading.Thread, threading.Event]] = {}
        self.forced_kills = 0
        self._bash_path: Path | None = None

    @property
    def execute_script(self) -> Path:
        return Path(__file__).with_name("execute-code.sh").resolve()

    def _bash(self) -> Path:
        if self._bash_path is None:
            self._bash_path = resolve_bash()
            preflight_bash(self._bash_path)
        return self._bash_path

    def _record_path(self, worker_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", worker_id):
            raise BranchError(f"Invalid worker ID: {worker_id!r}")
        return self.registry_dir / f"{worker_id}.json"

    def _write_record(self, record: WorkerRecord) -> None:
        """Atomically replace this worker's file; workers never share a registry blob."""
        destination = self._record_path(record.worker_id)
        temporary = self.registry_dir / f".{record.worker_id}.{uuid.uuid4().hex}.tmp"
        try:
            _write_private(temporary, json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(temporary, destination)
            if os.name != "nt":
                try:
                    destination.chmod(0o600)
                except OSError:
                    pass
        finally:
            self._remove_path(temporary)

    def _read_record(self, worker_id: str) -> WorkerRecord:
        path = self._record_path(worker_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = WorkerRecord.from_dict(data)
        except FileNotFoundError as error:
            raise BranchError(f"Unknown worker ID: {worker_id}") from error
        except Exception as error:
            raise BranchError(f"Worker registry record is invalid: {error}") from error
        return record

    def list_workers(self) -> list[WorkerRecord]:
        records: list[WorkerRecord] = []
        for path in sorted(self.registry_dir.glob("*.json")):
            try:
                record = WorkerRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                # A partial/corrupt record is never used to contact a server.
                self._remove_path(path)
                continue
            if record.server_pid is None:
                if record.status == "starting" and process_matches(record.starter_pid, record.starter_identity):
                    records.append(record)
                elif _original_process_gone(record.starter_pid, record.starter_identity):
                    if not self._cleanup_record(record):
                        records.append(record)
                else:
                    record.status = "stopping"
                    self._write_record(record)
                    records.append(record)
                continue
            if not process_matches(record.server_pid, record.process_identity):
                if _original_process_gone(record.server_pid, record.process_identity):
                    if not self._cleanup_record(record):
                        records.append(record)
                else:
                    record.status = "stopping"
                    self._write_record(record)
                    records.append(record)
                continue
            records.append(record)
        return records

    def get_worker(self, worker_id: str) -> WorkerRecord:
        return self._read_record(worker_id)

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

    def _parent_token(self) -> str:
        return parent_token(self.parent_token_file)

    def _redaction_tokens(self, *tokens: str) -> list[str]:
        """Secrets to scrub from errors: the given tokens plus the parent token, if resolvable."""
        try:
            known = self._parent_token()
        except BranchError:
            known = ""
        return [*tokens, known]

    @staticmethod
    def read_token(record: WorkerRecord) -> str:
        try:
            return record.token_path.read_text(encoding="utf-8")
        except OSError as error:
            raise BranchError(f"Worker credential file is unavailable: {error}") from error

    def _remove_path(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _remove_tree(self, path: Path) -> bool:
        for attempt in range(8):
            try:
                shutil.rmtree(path)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                if os.name != "nt" or attempt == 7:
                    return False
                time.sleep(0.1 * (attempt + 1))
        return False

    def _cleanup_record(self, record: WorkerRecord) -> bool:
        self._stop_drainer(record.worker_id)
        if not self._remove_tree(record.temp_dir):
            record.status = "stopping"
            self._write_record(record)
            return False
        self._remove_path(self._record_path(record.worker_id))
        self._processes.pop(record.worker_id, None)
        return True

    def _validate_parent_url(self, url: str) -> str:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as error:
            raise BranchError(f"Invalid parent URL: {error}") from error
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            raise BranchError("Parent server must be an HTTP loopback URL")
        if parsed.username or parsed.password:
            raise BranchError("Parent URL must not contain credentials")
        host = parsed.hostname.rstrip(".").lower()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise BranchError("Only a loopback parent server is supported")
        base = urllib.parse.urlunsplit(("http", parsed.netloc, parsed.path.rstrip("/"), "", ""))
        return base

    def _http_request(
        self,
        url: str,
        token: str | None = None,
        *,
        session: str | None = None,
        method: str = "GET",
        data: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if session is not None:
            headers["Marimo-Session-Id"] = session
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.request_timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                body = error.read()
            except OSError:
                body = b""
            return int(error.code), body
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise BranchError(f"Local HTTP request failed ({method} {urllib.parse.urlsplit(url).path}): {error}") from error

    def _sessions(self, base_url: str, token: str | None) -> tuple[int, dict[str, Any] | None]:
        status, body = self._http_request(base_url + "/api/sessions", token)
        if status < 200 or status >= 300:
            return status, None
        try:
            sessions = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise BranchError(f"Session endpoint returned invalid JSON: {error}") from error
        if not isinstance(sessions, dict):
            raise BranchError("Session endpoint did not return a session mapping")
        return status, sessions

    def _validate_parent(self, parent: ParentTarget) -> str:
        base = self._validate_parent_url(parent.url)
        if not parent.session_id or "\n" in parent.session_id:
            raise BranchError("A current parent session ID is required")
        token = self._parent_token()
        notebook = _real_path(parent.notebook_path)
        if not notebook.is_file() or not os.access(notebook, os.R_OK):
            raise BranchError(f"Parent notebook is inaccessible: {notebook}")
        # A Windows path interpreted on a POSIX host (or vice versa) is never accepted.
        if os.name != "nt" and PureWindowsPath(str(parent.notebook_path)).is_absolute():
            raise BranchError("Parent notebook path belongs to a different operating-system path boundary")
        unauth_status, unauth = self._sessions(base, None)
        if 200 <= unauth_status < 300:
            raise BranchError("Parent /api/sessions unexpectedly accepts unauthenticated requests")
        auth_status, sessions = self._sessions(base, token)
        if not 200 <= auth_status < 300 or sessions is None:
            raise BranchError("Could not authenticate to the parent session list")
        entry = sessions.get(parent.session_id)
        if entry is None:
            raise BranchError(f"Parent session {parent.session_id!r} is not active")
        path = _session_path(entry)
        if not path or not _same_notebook_path(path, notebook):
            raise BranchError("Parent session does not map to the requested real notebook path")
        return str(notebook)

    def _new_record(self, parent: ParentTarget, parent_notebook: str, names: Sequence[str]) -> WorkerRecord:
        worker_id = uuid.uuid4().hex
        temp_dir = self.temp_root / worker_id
        try:
            _private_mkdir(temp_dir)
            port = self._available_port()
            token_path = temp_dir / "worker-token"
            token = secrets.token_urlsafe(32)
            _write_private(token_path, token)
        except BaseException:
            self._remove_tree(temp_dir)
            raise
        return WorkerRecord(
            worker_id=worker_id,
            url=f"http://127.0.0.1:{port}",
            session_id=uuid.uuid4().hex,
            token_path=token_path,
            notebook_path=temp_dir / "worker.py",
            temp_dir=temp_dir,
            runtime_path=temp_dir / "runtime.json",
            parent_url=self._validate_parent_url(parent.url),
            parent_notebook_path=Path(parent_notebook),
            parent_session_id=parent.session_id,
            status="starting",
            server_pid=None,
            process_identity=None,
            imported_names=tuple(names),
            log_path=temp_dir / "worker.log",
            bundle_path=temp_dir / "values.pickle",
            helper_path=temp_dir / "branch_state.py",
            starter_pid=os.getpid(),
            starter_identity=process_identity(os.getpid()),
        )

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _export_parent(self, record: WorkerRecord, parent: ParentTarget, names: Sequence[str]) -> None:
        bash = self._bash()
        helper = Path(branch_state.__file__).resolve()
        code = build_parent_export_code(
            names, record.bundle_path, record.runtime_path, helper,
            record.parent_notebook_path,
        )
        token = self._parent_token()
        env = _without_token_sources(os.environ)
        env["MARIMO_TOKEN"] = token
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            str(bash), _path_text(self.execute_script),
            "--url", record.parent_url, "--session", parent.session_id, "-",
        ]
        try:
            result = subprocess.run(
                command, input=code, text=True, encoding="utf-8", capture_output=True,
                timeout=self.execution_timeout,
                cwd=str(record.parent_notebook_path.parent), env=env, close_fds=True,
            )
        except subprocess.TimeoutExpired as error:
            raise BranchError("Timed out exporting the named values from the parent") from error
        except OSError as error:
            raise BranchError(f"Could not execute the parent exporter: {error}") from error
        if result.returncode != 0 or not record.runtime_path.is_file() or not record.bundle_path.is_file():
            detail = redact_log_text((result.stderr or result.stdout or "exporter produced no artifacts")[-2000:], [token])
            raise BranchError(f"Parent value export failed: {detail}")

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

    def _server_command(self, record: WorkerRecord, runtime: ParentRuntime) -> list[str]:
        port = urllib.parse.urlsplit(record.url).port
        assert port is not None
        return [
            runtime.python_executable, "-m", "marimo", "-q", "edit", str(record.notebook_path),
            "--headless", "--host", "127.0.0.1", "--port", str(port),
            "--token-password-file", str(record.token_path),
            "--no-skew-protection", "--no-sandbox", "--skip-update-check",
        ]

    def _child_environment(self) -> dict[str, str]:
        env = _without_token_sources(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _spawn(self, record: WorkerRecord, runtime: ParentRuntime) -> subprocess.Popen[bytes]:
        command = self._server_command(record, runtime)
        env = self._child_environment()
        kwargs: dict[str, Any] = {
            "cwd": runtime.cwd,
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            kwargs["start_new_session"] = True
        with record.log_path.open("ab") as log:
            kwargs["stdout"] = log
            process = subprocess.Popen(command, **kwargs)
        # The Popen handle is transient; only PID and creation identity are persisted.
        self._processes[record.worker_id] = process
        record.server_pid = process.pid
        deadline = time.monotonic() + 2
        identity = process_identity(process.pid)
        while identity is None and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
            identity = process_identity(process.pid)
        if identity is None:
            raise BranchError("Could not establish worker process creation identity")
        record.process_identity = identity
        record.status = "starting"
        self._write_record(record)
        return process

    def _read_log_tail(self, record: WorkerRecord, token: str) -> str:
        try:
            text = record.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return redact_log_text(
            "\n".join(text.splitlines()[-40:]),
            self._redaction_tokens(token),
        )

    def _assert_unauthenticated_rejected(self, base: str) -> None:
        status, _ = self._sessions(base, None)
        if 200 <= status < 300:
            raise BranchError("Worker /api/sessions unexpectedly accepts unauthenticated requests")

    def _wait_fresh_server(self, record: WorkerRecord, token: str) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not process_matches(record.server_pid, record.process_identity):
                tail = self._read_log_tail(record, token)
                raise BranchError(f"Worker process exited before opening its API. Redacted log tail:\n{tail}")
            try:
                self._assert_unauthenticated_rejected(record.url)
                status, sessions = self._sessions(record.url, token)
                if 200 <= status < 300 and sessions == {}:
                    return
                if sessions:
                    raise BranchError("New worker unexpectedly has a pre-existing session")
            except BranchError as error:
                if "unauthenticated" in str(error) or "pre-existing" in str(error):
                    raise
                last_error = error
            time.sleep(0.1)
        tail = self._read_log_tail(record, token)
        detail = f"; last request error: {last_error}" if last_error else ""
        raise BranchError(f"Timed out waiting for an authenticated empty worker API{detail}. Redacted log tail:\n{tail}")

    def _open_sse(self, record: WorkerRecord, token: str, *, timeout: float | None = None):
        # The file query is intentionally omitted: each worker has one file.
        url = record.url + "/sse?session_id=" + urllib.parse.quote(record.session_id, safe="")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        stream_timeout = timeout or self.request_timeout
        try:
            response = urllib.request.urlopen(request, timeout=stream_timeout)
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.settimeout(stream_timeout)
            return response
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise BranchError(f"Could not open worker SSE stream: {error}") from error

    @staticmethod
    def _response_lines(response) -> Iterator[bytes]:
        while True:
            line = response.readline()
            if not line:
                return
            yield line

    def _validate_worker(self, record: WorkerRecord) -> None:
        if not process_matches(record.server_pid, record.process_identity):
            record.status = "stopping"
            self._write_record(record)
            raise BranchError(f"Worker {record.worker_id} process identity no longer matches; worker is lost")
        token = self.read_token(record)
        self._assert_unauthenticated_rejected(record.url)
        status, sessions = self._sessions(record.url, token)
        if not 200 <= status < 300 or sessions is None:
            record.status = "stopping"
            self._write_record(record)
            raise BranchError(f"Worker {record.worker_id} session identity check failed; worker is lost")
        entry = sessions.get(record.session_id)
        path = _session_path(entry)
        if entry is None or not path or not _same_notebook_path(path, record.notebook_path):
            record.status = "stopping"
            self._write_record(record)
            raise BranchError(f"Worker {record.worker_id} session no longer maps to its notebook; worker is lost")

    def _validate_runtime_code(self, record: WorkerRecord, runtime: ParentRuntime) -> str:
        notebook = repr(str(record.notebook_path))
        helper = repr(str(record.helper_path))
        metadata = repr(str(record.runtime_path))
        expected_pythonpath = repr(list(runtime.pythonpath))
        expected_dotenv = repr(list(runtime.dotenv))
        return f'''import importlib.util as _bp_util
import json as _bp_json
import os as _bp_os
import sys as _bp_sys
from marimo._config.manager import get_default_config_manager as _bp_config_manager
_bp_helper_name = "_marimo_pair_runtime_check"
_bp_spec = _bp_util.spec_from_file_location(_bp_helper_name, {helper})
_bp_helper = _bp_util.module_from_spec(_bp_spec)
_bp_sys.modules[_bp_helper_name] = _bp_helper
_bp_spec.loader.exec_module(_bp_helper)
_bp_runtime = _bp_helper.read_parent_runtime(_bp_os.path.abspath({metadata}))
_bp_helper.verify_runtime_environment(_bp_runtime)
_bp_config = _bp_config_manager(current_path={notebook}).get_config(hide_secrets=False)
_bp_runtime_config = _bp_config.get("runtime", {{}})
_bp_pythonpath = [str(_bp_os.path.abspath(path)) for path in _bp_runtime_config.get("pythonpath", [])]
_bp_dotenv = [str(_bp_os.path.abspath(path)) for path in _bp_runtime_config.get("dotenv", [])]
_bp_expected_pythonpath = {expected_pythonpath}
_bp_expected_dotenv = {expected_dotenv}
if _bp_pythonpath != _bp_expected_pythonpath:
    raise RuntimeError("worker runtime.pythonpath differs from captured parent paths: " + repr(_bp_pythonpath))
if _bp_dotenv != _bp_expected_dotenv:
    raise RuntimeError("worker runtime.dotenv differs from captured parent paths: " + repr(_bp_dotenv))
_bp_sys_paths = {{_bp_os.path.normcase(_bp_os.path.abspath(path or _bp_os.getcwd())) for path in _bp_sys.path}}
for _bp_path in _bp_expected_pythonpath:
    if _bp_os.path.normcase(_bp_os.path.abspath(_bp_path)) not in _bp_sys_paths:
        raise RuntimeError("worker import root is not active in sys.path: " + _bp_path)
print("MARIMO_PAIR_RUNTIME_OK")
'''

    def _bootstrap_code(self, record: WorkerRecord, names: Sequence[str]) -> str:
        cell = build_worker_bootstrap_cell(names, record.bundle_path, record.runtime_path, record.helper_path)
        name_list = repr(list(names))
        # Assert in the worker cell's own namespace; scratchpad globals are not
        # the notebook cell namespace and cannot prove its registered bindings.
        cell += f"\nassert set({name_list}).issubset(locals().keys())\n"
        quoted_cell = repr(cell)
        # The context exit waits for all queued cell executions to finish.
        return f'''import json as _bp_json
import marimo._code_mode as cm
async with cm.get_context() as _bp_ctx:
    _bp_cell_id = _bp_ctx.create_cell({quoted_cell})
    _bp_ctx.run_cell(_bp_cell_id)
_bp_status = str(_bp_ctx.cells[_bp_cell_id].status)
print("MARIMO_PAIR_BOOTSTRAP:" + _bp_json.dumps({{"status": _bp_status, "cell_id": _bp_cell_id}}))
'''

    def _open_drainer(self, record: WorkerRecord, token: str) -> None:
        self._validate_worker(record)
        response = self._open_sse(record, token, timeout=max(self.request_timeout, 30.0))
        stop = threading.Event()

        def drain() -> None:
            try:
                while not stop.is_set():
                    try:
                        line = response.readline()
                    except socket.timeout:
                        continue
                    if not line:
                        return
            except (OSError, TimeoutError):
                if not stop.is_set():
                    return
            finally:
                try:
                    response.close()
                except OSError:
                    pass

        thread = threading.Thread(target=drain, name=f"marimo-pair-drain-{record.worker_id[:8]}", daemon=True)
        self._drainers[record.worker_id] = (response, thread, stop)
        thread.start()

    def _stop_drainer(self, worker_id: str) -> None:
        drainer = self._drainers.pop(worker_id, None)
        if drainer is None:
            return
        response, thread, stop = drainer
        stop.set()
        try:
            response.close()
        except OSError:
            pass
        thread.join(timeout=2)

    def _run_required(self, record: WorkerRecord, code: str, purpose: str) -> ExecutionResult:
        result = self._execute_record(record, code, validate=True)
        if not result.success and purpose == "code-mode initialization":
            # Some installed versions treat the initial SSE disconnect as session
            # loss. Keep a bounded active drainer and retry this side-effect-free
            # initialization once; never replay bootstrap code that may have added a cell.
            self._open_drainer(record, self.read_token(record))
            result = self._execute_record(record, code, validate=True)
        if not result.success:
            token = self.read_token(record)
            detail = redact_log_text(
                result.stderr or result.output,
                self._redaction_tokens(token),
            )[-2000:]
            raise BranchError(f"Worker {purpose} failed: {detail}")
        return result

    def _launch_and_initialize(self, record: WorkerRecord, runtime: ParentRuntime) -> None:
        token = self.read_token(record)
        last_error: Exception | None = None
        for attempt in range(5):
            process = self._spawn(record, runtime)
            try:
                self._wait_fresh_server(record, token)
                # Fresh API must still be empty and protected immediately before SSE.
                if not process_matches(record.server_pid, record.process_identity):
                    raise BranchError("Worker process identity changed before SSE connection")
                self._assert_unauthenticated_rejected(record.url)
                status, sessions = self._sessions(record.url, token)
                if not 200 <= status < 300 or sessions != {}:
                    raise BranchError("Worker session list was not empty immediately before SSE")
                response = self._open_sse(record, token, timeout=self.startup_timeout)
                try:
                    wait_for_kernel_ready(self._response_lines(response), self.startup_timeout)
                finally:
                    response.close()
                self._validate_worker(record)
                # The first code-mode operation must succeed before any cm APIs are used.
                self._run_required(record, "import marimo._code_mode as cm; help(cm)", "code-mode initialization")
                self._run_required(record, self._validate_runtime_code(record, runtime), "runtime verification")
                bootstrap = self._run_required(record, self._bootstrap_code(record, record.imported_names), "value bootstrap")
                marker = next((line for line in bootstrap.output.splitlines() if line.startswith("MARIMO_PAIR_BOOTSTRAP:")), None)
                if marker is None:
                    raise BranchError("Worker bootstrap did not report registered names")
                try:
                    result = json.loads(marker.partition(":")[2])
                except ValueError as error:
                    raise BranchError("Worker bootstrap result was invalid") from error
                if result.get("status") != "idle":
                    raise BranchError(f"Worker bootstrap cell did not complete: {result}")
                restored = self._inspect_worker_names(record)
                missing = set(record.imported_names) - restored
                if missing:
                    raise BranchError(f"Worker bootstrap did not register requested names: {sorted(missing)}")
                # A fallback drainer remains active through worker shutdown.
                return
            except BaseException as error:
                last_error = error
                # A binding collision can happen between selecting and binding the port.
                if process.poll() is not None and attempt < 4:
                    tail = self._read_log_tail(record, token).lower()
                    if "address already in use" in tail or "only one usage of each socket address" in tail:
                        self._retire_spawn(record, process, force=True)
                        record.url = f"http://127.0.0.1:{self._available_port()}"
                        record.server_pid = None
                        record.process_identity = None
                        self._write_record(record)
                        continue
                self._retire_spawn(record, process, force=True)
                if isinstance(error, BranchError) and "Redacted log tail:" not in str(error):
                    detail = redact_log_text(str(error), self._redaction_tokens(token))
                    tail = self._read_log_tail(record, token)
                    if tail:
                        raise BranchError(f"{detail}\nRedacted log tail:\n{tail}") from error
                raise
        raise BranchError(f"Could not launch worker server: {last_error}") from last_error

    def _inspect_worker_names(self, record: WorkerRecord) -> set[str]:
        code = '''import json as _bp_json
import marimo._code_mode as cm
async with cm.get_context() as _bp_ctx:
    _bp_names = sorted({name for _bp_impl in _bp_ctx.graph.cells.values() for name in _bp_impl.defs})
print("MARIMO_PAIR_WORKER_DEFS:" + _bp_json.dumps(_bp_names))
'''
        result = self._execute_record(record, code, validate=True)
        marker = next((line for line in result.output.splitlines() if line.startswith("MARIMO_PAIR_WORKER_DEFS:")), None)
        if not result.success or marker is None:
            detail = result.stderr or result.output or "worker definitions could not be inspected"
            raise BranchError(f"Could not inspect worker definitions before add: {detail[-1000:]}")
        try:
            names = json.loads(marker.partition(":")[2])
        except ValueError as error:
            raise BranchError("Worker definition response was invalid") from error
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise BranchError("Worker definition response was invalid")
        return set(names)

    def _add_bootstrap_code(self, record: WorkerRecord, names: Sequence[str]) -> str:
        cell = build_worker_bootstrap_cell(
            names, record.bundle_path, record.runtime_path, record.helper_path
        )
        return f'''import json as _bp_json
import marimo._code_mode as cm
_bp_cell_id = None
try:
    async with cm.get_context() as _bp_ctx:
        _bp_cell_id = _bp_ctx.create_cell({cell!r})
        _bp_ctx.run_cell(_bp_cell_id)
except BaseException:
    if _bp_cell_id is not None:
        try:
            async with cm.get_context() as _bp_cleanup_ctx:
                _bp_cleanup_ctx.delete_cell(_bp_cell_id)
        except BaseException:
            pass
    raise
async with cm.get_context() as _bp_ctx:
    _bp_status = str(_bp_ctx.cells[_bp_cell_id].status)
_bp_result = {{"status": _bp_status, "cell_id": _bp_cell_id}}
print("MARIMO_PAIR_BOOTSTRAP:" + _bp_json.dumps(_bp_result))
'''

    def _delete_worker_cell(self, record: WorkerRecord, cell_id: str) -> None:
        code = f'''import marimo._code_mode as cm
async with cm.get_context() as _bp_cleanup_ctx:
    _bp_cleanup_ctx.delete_cell({cell_id!r})
'''
        result = self._execute_record(record, code, validate=True)
        if not result.success:
            raise BranchError(f"Could not roll back failed bootstrap cell {cell_id!r}: {result.stderr or result.output}")

    def add_worker(self, worker_id: str, parent_session_id: str | None, names: Sequence[str]) -> None:
        """Snapshot a fresh named parent batch into an existing worker."""
        record = self._read_record(worker_id)
        if record.status != "running":
            raise BranchError(f"Worker {worker_id} is not ready for additions")
        # Validate syntax and known bootstrap names before contacting either server.
        try:
            selected = validate_names(names, record.imported_names)
        except ValueError as error:
            raise BranchError(str(error)) from error
        self._validate_worker(record)
        worker_definitions = self._inspect_worker_names(record)
        try:
            selected = validate_names(selected, worker_definitions | set(record.imported_names))
        except ValueError as error:
            raise BranchError(str(error)) from error

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
        self._write_record(record)

        try:
            # Each batch is independent; private paths are reused only after the
            # previous batch's artifacts have been removed.
            for path in (record.bundle_path, record.runtime_path, record.helper_path):
                self._remove_path(path)
            shutil.copyfile(Path(branch_state.__file__).resolve(), record.helper_path)
            if os.name != "nt":
                record.helper_path.chmod(0o600)
            self._export_parent(record, parent, selected)
            runtime = read_parent_runtime(record.runtime_path)
            if not _same_notebook_path(runtime.notebook_path, record.parent_notebook_path):
                raise BranchError("Parent runtime metadata identifies a different notebook")
            runtime_check = self._execute_record(
                record, self._validate_runtime_code(record, runtime), validate=True
            )
            if not runtime_check.success:
                raise BranchError(f"Parent runtime no longer matches this worker: {runtime_check.stderr or runtime_check.output}")
            result = self._execute_record(record, self._add_bootstrap_code(record, selected), validate=True)
            marker = next((line for line in result.output.splitlines() if line.startswith("MARIMO_PAIR_BOOTSTRAP:")), None)
            status = None
            if marker is not None:
                try:
                    status = json.loads(marker.partition(":")[2])
                except ValueError as error:
                    raise BranchError("Worker bootstrap result was invalid") from error
            if not result.success or status is None or status.get("status") != "idle":
                rollback_error = None
                if isinstance(status, dict) and status.get("cell_id"):
                    try:
                        self._delete_worker_cell(record, str(status["cell_id"]))
                    except BranchError as error:
                        rollback_error = error
                detail = result.stderr or result.output or f"worker bootstrap did not complete: {status}"
                if rollback_error is not None:
                    detail = f"{detail}; rollback failed: {rollback_error}"
                raise BranchError(f"Could not add values to worker {worker_id}: {detail[-1500:]}")
            try:
                restored_definitions = self._inspect_worker_names(record)
            except BranchError as error:
                try:
                    self._delete_worker_cell(record, str(status["cell_id"]))
                except BranchError as rollback_error:
                    raise BranchError(f"Could not verify added names and rollback failed: {rollback_error}") from error
                raise BranchError(f"Could not verify added names: {error}") from error
            missing = set(selected) - restored_definitions
            if missing:
                try:
                    self._delete_worker_cell(record, str(status["cell_id"]))
                except BranchError as rollback_error:
                    raise BranchError(f"Bootstrap omitted {sorted(missing)} and rollback failed: {rollback_error}")
                raise BranchError(f"Worker bootstrap did not define requested names: {sorted(missing)}")
            record.imported_names = tuple((*record.imported_names, *selected))
            self._write_record(record)
        finally:
            for path in (record.bundle_path, record.runtime_path, record.helper_path):
                self._remove_path(path)

    def _execute_record(self, record: WorkerRecord, code: str, *, validate: bool) -> ExecutionResult:
        if validate:
            self._validate_worker(record)
        bash = self._bash()
        token = self.read_token(record)
        env = _without_token_sources(os.environ)
        env["MARIMO_TOKEN"] = token
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            str(bash), _path_text(self.execute_script), "--url", record.url,
            "--session", record.session_id, "-",
        ]
        try:
            completed = subprocess.run(
                command, input=code, text=True, encoding="utf-8", capture_output=True,
                timeout=self.execution_timeout, env=env, close_fds=True,
            )
            return ExecutionResult(
                completed.returncode == 0,
                redact_log_text(completed.stdout, self._redaction_tokens(token)),
                redact_log_text(completed.stderr, self._redaction_tokens(token)),
                completed.returncode,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            return ExecutionResult(
                False,
                redact_log_text(stdout, self._redaction_tokens(token)),
                redact_log_text(stderr, self._redaction_tokens(token)),
                -1,
            )
        except OSError as error:
            return ExecutionResult(False, "", str(error), -1)

    def execute(self, worker_id: str, code: str, *, record: WorkerRecord | None = None) -> ExecutionResult:
        """Execute code against one explicit worker ID, URL/session, and token."""
        selected = record or self._read_record(worker_id)
        if selected.worker_id != worker_id:
            raise BranchError("Worker ID does not match the selected registry record")
        if selected.status != "running":
            raise BranchError(f"Worker {worker_id} is not ready for execution (status={selected.status})")
        return self._execute_record(selected, code, validate=True)

    def _wait_for_server_exit(self, record: WorkerRecord, timeout: float | None = None) -> bool:
        if record.server_pid is None:
            return True
        process = self._processes.get(record.worker_id)
        deadline = self.shutdown_timeout if timeout is None else timeout
        if process is not None:
            try:
                process.wait(timeout=max(0, deadline))
                return True
            except subprocess.TimeoutExpired:
                return False
        return wait_for_process(record.server_pid, record.process_identity, deadline)

    def _terminate_if_same_process(self, record: WorkerRecord) -> bool:
        pid = record.server_pid
        if pid is None:
            return True
        current_identity = process_identity(pid)
        if current_identity is None:
            return _pid_alive_state(pid) is False
        if record.process_identity is None:
            return False
        if current_identity != record.process_identity:
            return True
        # Revalidate creation identity directly before any forced termination.
        if process_identity(pid) != record.process_identity:
            return _original_process_gone(pid, record.process_identity)
        self.forced_kills += 1
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                if result.returncode != 0 and process_matches(pid, record.process_identity):
                    return False
            except (OSError, subprocess.TimeoutExpired):
                return False
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    return not process_matches(pid, record.process_identity)
        return wait_for_process(pid, record.process_identity, 5)

    def _retire_spawn(self, record: WorkerRecord, process: subprocess.Popen[bytes], *, force: bool) -> None:
        if process.poll() is None and force:
            if record.server_pid and process_matches(record.server_pid, record.process_identity):
                self._terminate_if_same_process(record)
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        self._stop_drainer(record.worker_id)

    def _shutdown(self, record: WorkerRecord) -> None:
        self._validate_worker(record)
        record.status = "stopping"
        self._write_record(record)
        token = self.read_token(record)
        payload = b"{}"
        try:
            status, body = self._http_request(
                record.url + "/api/kernel/shutdown", token, session=record.session_id,
                method="POST", data=payload,
            )
        except BranchError:
            status, body = 0, b""
        shutdown_error: BranchError | None = None
        if not 200 <= status < 300:
            safe_body = redact_log_text(
                body[:500].decode("utf-8", errors="replace"),
                self._redaction_tokens(token),
            )
            shutdown_error = BranchError(f"Worker shutdown endpoint returned HTTP {status}: {safe_body}")
        self._stop_drainer(record.worker_id)
        if not self._wait_for_server_exit(record, self.shutdown_timeout):
            # Process creation identity is checked inside the force path.
            if not self._terminate_if_same_process(record):
                raise BranchError("Worker did not exit after shutdown and could not be safely terminated")
        if shutdown_error is not None:
            raise shutdown_error

    def stop_worker(self, worker_id: str) -> None:
        record = self._read_record(worker_id)
        if record.server_pid is None:
            if record.status == "starting" and process_matches(record.starter_pid, record.starter_identity):
                raise BranchError(f"Worker {worker_id} is still starting; stop it after startup completes")
            if _original_process_gone(record.starter_pid, record.starter_identity):
                if not self._cleanup_record(record):
                    raise BranchError(f"Could not remove worker {worker_id} temporary files")
                return
            record.status = "stopping"
            self._write_record(record)
            raise BranchError(f"Worker {worker_id} starter identity is unknown; refusing to remove its files")
        try:
            self._shutdown(record)
        except BranchError as error:
            # Validation failed before shutdown could safely contact the server.
            # If the recorded process is still ours, terminate only that verified
            # process; if identity/liveness is uncertain, retain its recovery data.
            if _original_process_gone(record.server_pid, record.process_identity):
                if not self._cleanup_record(record):
                    raise BranchError(f"Could not remove stale worker {worker_id} temporary files") from error
            elif process_matches(record.server_pid, record.process_identity):
                if not self._terminate_if_same_process(record):
                    raise BranchError(
                        f"Worker {worker_id} could not be safely terminated after validation failed; "
                        "its record and files were retained"
                    ) from error
                if not self._cleanup_record(record):
                    raise BranchError(
                        f"Worker {worker_id} terminated, but its private files could not be removed"
                    ) from error
            raise
        if not self._cleanup_record(record):
            raise BranchError(f"Worker exited, but its private temporary files could not be removed: {record.temp_dir}")

    def stop_all(self) -> None:
        for record in self.list_workers():
            try:
                self.stop_worker(record.worker_id)
            except BranchError:
                continue

    def _abort_worker(self, record: WorkerRecord) -> bool:
        process = self._processes.get(record.worker_id)
        if record.server_pid is not None:
            if process_matches(record.server_pid, record.process_identity):
                # Never probe/kill a reused PID; prefer protocol shutdown only after identity validation.
                try:
                    self._validate_worker(record)
                except BranchError:
                    pass
                else:
                    try:
                        status, _ = self._http_request(
                            record.url + "/api/kernel/shutdown", self.read_token(record),
                            session=record.session_id, method="POST", data=b"{}",
                        )
                        if 200 <= status < 300:
                            self._wait_for_server_exit(record, min(self.shutdown_timeout, 3))
                    except BranchError:
                        pass
                if process_matches(record.server_pid, record.process_identity):
                    self._terminate_if_same_process(record)
            if not _original_process_gone(record.server_pid, record.process_identity):
                record.status = "stopping"
                self._write_record(record)
                return False
        if process is not None:
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass
        return self._cleanup_record(record)


def _write_cli_text(stream: Any, text: str) -> None:
    """Write user-visible execution output as UTF-8 regardless of Windows ANSI code page."""
    if not text:
        return
    data = text.encode("utf-8")
    binary = getattr(stream, "buffer", None)
    if binary is not None and callable(getattr(binary, "write", None)):
        binary.write(data)
        flush = getattr(binary, "flush", None)
        if callable(flush):
            flush()
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    stream.write(data.decode("utf-8"))


def _worker_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"--count must be an integer, got {value!r}") from error
    if not 1 <= count <= MAX_WORKERS_PER_START:
        raise argparse.ArgumentTypeError(f"--count must be between 1 and {MAX_WORKERS_PER_START}")
    return count


def main(argv: Sequence[str] | None = None) -> int:
    """Coordinator CLI; the run subcommand accepts only a worker ID and stdin."""
    parser = argparse.ArgumentParser(description="Manage local marimo-pair branch workers")
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    start.add_argument(
        "--count", type=_worker_count,
        help=f"start N workers from one export of the parent (1-{MAX_WORKERS_PER_START}); prints a JSON list",
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

    run = subparsers.add_parser("run", help="execute stdin code in one explicit worker")
    run.add_argument("--worker", required=True, help="worker ID; code is read from stdin")
    run.add_argument("source", choices=["-"], help="'-' to read code from stdin")

    subparsers.add_parser("list", help="list coordinator-owned workers")
    stop = subparsers.add_parser("stop", help="stop one worker")
    stop.add_argument("--worker", required=True, help="worker ID to stop")

    args = parser.parse_args(argv)
    manager = WorkerManager()
    manager.parent_token_file = getattr(args, "token_file", None)
    try:
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
        if args.command == "add":
            manager.add_worker(args.worker, args.parent_session, args.names)
            print("Added values from the parent's current state; later batches may reflect newer parent values.")
            return 0
        if args.command == "run":
            # A timed-out transport cannot cancel code already running in the
            # worker; avoid reporting failure and inviting duplicate side effects.
            manager.execution_timeout = None
            stdin_buffer = getattr(sys.stdin, "buffer", None)
            code = stdin_buffer.read().decode("utf-8") if stdin_buffer is not None else sys.stdin.read()
            result = manager.execute(args.worker, code)
            if result.output:
                _write_cli_text(sys.stdout, result.output + ("" if result.output.endswith("\n") else "\n"))
            if result.stderr:
                _write_cli_text(sys.stderr, result.stderr + ("" if result.stderr.endswith("\n") else "\n"))
            return result.returncode if result.returncode != 0 else (0 if result.success else 1)
        if args.command == "list":
            print(json.dumps([
                {
                    "worker_id": record.worker_id,
                    "url": record.url,
                    "session_id": record.session_id,
                    "status": record.status,
                    "imported_names": list(record.imported_names),
                }
                for record in manager.list_workers()
            ]))
            return 0
        manager.stop_worker(args.worker)
        return 0
    except (BranchError, ValueError, OSError) as error:
        print(f"branch.py {args.command} failed: {error}", file=sys.stderr)
        return 1


__all__ = [
    "BranchError", "ExecutionResult", "ParentTarget", "SSEEvent", "WorkerManager",
    "WorkerRecord", "main", "parse_sse_records", "preflight_bash", "process_identity",
    "process_matches", "redact_log_text", "resolve_bash", "wait_for_kernel_ready",
    "wait_for_process",
]


if __name__ == "__main__":
    raise SystemExit(main())
