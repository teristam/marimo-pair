"""Live-notebook harness for marimo-pair agent evals.

Run with the notebook environment's interpreter (tests/notebooks/.venv/bin/python):

  harness.py start   --workspace DIR [--notebook PATH]  start a token-protected server on a copy
  harness.py inspect --workspace DIR                    print the graded end state as JSON
  harness.py stop    --workspace DIR                    stop leftover workers and the server

`start` writes DIR/server.json (url, notebook, token_file, pair_home) for the eval
prompt. `inspect` is what the grader reads: kernel cells with status/errors,
whether the .py on disk still matches the kernel (a direct file edit breaks
this), which original cells changed, and branch workers still registered.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.test_branch_integration import ParentServer, branch  # noqa: E402

DEFAULT_NOTEBOOK = REPO_ROOT / "tests" / "notebooks" / "analysis.py"
CELLS_CODE = """
import json as _h_json
import marimo._code_mode as cm
async with cm.get_context() as _h_ctx:
    _h_cells = [
        {"id": _c.id, "name": _c.name, "code": _c.code, "status": str(_c.status),
         "errors": [str(_e) for _e in (_c.errors or [])]}
        for _c in _h_ctx.cells
    ]
print("HARNESS_CELLS:" + _h_json.dumps(_h_cells, default=str))
"""


def _disk_cells(notebook: Path) -> list[str]:
    from marimo._ast.load import load_app

    return [code.strip() for code in load_app(str(notebook))._cell_manager.codes()]


def start(workspace: Path, notebook_source: Path, ready: tuple[str, ...]) -> dict:
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    notebook = workspace / "notebook" / notebook_source.name
    notebook.parent.mkdir(exist_ok=True)
    shutil.copyfile(notebook_source, notebook)
    parent = ParentServer(workspace, notebook, None, python=sys.executable,
                          ready_names=ready, extra_args=("--session-ttl", "86400"))
    parent.start()
    if parent.sse is not None:
        parent.sse.close()
    info = {
        "url": parent.url,
        "notebook": str(notebook),
        "token_file": str(parent.token_path),
        "session": parent.session,
        "pid": parent.process.pid,
        "pair_home": str(Path(tempfile.gettempdir()) / f"marimo-pair-eval-{hashlib.sha1(str(workspace).encode()).hexdigest()[:12]}"),
        "original_cells": _disk_cells(notebook),
    }
    (workspace / "server.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return {k: v for k, v in info.items() if k != "original_cells"}


def _load(workspace: Path) -> dict:
    return json.loads((workspace.resolve() / "server.json").read_text(encoding="utf-8"))


def inspect(workspace: Path) -> dict:
    info = _load(workspace)
    env = os.environ.copy()
    env["MARIMO_TOKEN"] = Path(info["token_file"]).read_text(encoding="utf-8").strip()
    done = subprocess.run(
        [str(branch.resolve_bash()), str(REPO_ROOT / "skills" / "marimo-pair" / "scripts" / "execute-code.sh"),
         "--url", info["url"], "--session", info["session"], "-"],
        input=CELLS_CODE, text=True, capture_output=True, timeout=120, env=env,
    )
    line = next((l for l in done.stdout.splitlines() if l.startswith("HARNESS_CELLS:")), None)
    if line is None:
        raise SystemExit(f"Could not read kernel cells: {done.stderr or done.stdout}")
    cells = json.loads(line.partition(":")[2])
    kernel_codes = [cell["code"].strip() for cell in cells]
    disk_codes = _disk_cells(Path(info["notebook"]))
    original = info["original_cells"]
    workers = []
    if Path(info["pair_home"]).exists():
        listed = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "skills" / "marimo-pair" / "scripts" / "branch.py"), "list"],
            text=True, capture_output=True, timeout=60, env={**os.environ, "MARIMO_PAIR_HOME": info["pair_home"]},
        )
        workers = json.loads(listed.stdout or "[]")
    return {
        "cells": cells,
        "errored_cells": [cell["id"] for cell in cells if cell["errors"] or "error" in cell["status"].lower()],
        "disk_matches_kernel": disk_codes == kernel_codes,
        "original_cells_changed": [code[:80] for code in original if code not in kernel_codes],
        "new_cells": [code for code in kernel_codes if code not in original],
        "live_workers": [w for w in workers if w.get("status") not in {"stopped"}],
    }


def stop(workspace: Path) -> None:
    info = _load(workspace)
    if Path(info["pair_home"]).exists():
        branch.WorkerManager(root_dir=Path(info["pair_home"])).stop_all()
    try:
        os.kill(info["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["start", "inspect", "stop"])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--notebook", type=Path, default=DEFAULT_NOTEBOOK)
    parser.add_argument("--ready", nargs="*", default=["custom_model"],
                        help="globals that must exist before the notebook counts as loaded")
    args = parser.parse_args()
    if args.command == "start":
        print(json.dumps(start(args.workspace, args.notebook, tuple(args.ready)), indent=2))
    elif args.command == "inspect":
        print(json.dumps(inspect(args.workspace), indent=2))
    else:
        stop(args.workspace)


if __name__ == "__main__":
    main()
