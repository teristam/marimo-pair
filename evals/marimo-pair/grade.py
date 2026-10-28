"""Grade one marimo-pair eval run into grading.json (skill-creator schema).

  tests/notebooks/.venv/bin/python evals/marimo-pair/grade.py WORKSPACE/iteration-N/EVAL_NAME [--config with_skill]

Reads RUN/server (via harness.inspect), RUN/<config>/outputs/{transcript,answer}.md
and evals.json. Expectations with a programmatic check are graded here; the rest
are written with passed=null so a human or grader agent can fill them in.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harness  # noqa: E402

UNDERSCORE_REIMPORT = re.compile(r"^\s*(?:import\s+\w+(?:\.\w+)*\s+as\s+_\w+|from\s+\S+\s+import\s+.*\bas\s+_\w+)", re.M)
NUMPY_ESCAPE = re.compile(r"\.(?:values|to_numpy\(\)|data)\b")


def _first_kernel_command_is_help(transcript: str) -> tuple[bool, str]:
    calls = [m.start() for m in re.finditer(r"execute-code\.sh", transcript)]
    helps = [m.start() for m in re.finditer(r"help\(cm\)", transcript)]
    cm_use = [m.start() for m in re.finditer(r"cm\.get_context|_code_mode as cm\s*\n", transcript)]
    if not calls:
        return False, "no execute-code.sh call in transcript"
    first_help = min(helps) if helps else None
    first_cm = min(cm_use) if cm_use else None
    if first_help is None:
        return False, "help(cm) never run"
    ok = first_cm is None or first_help < first_cm
    return ok, f"help(cm) at offset {first_help}, first cm.get_context at {first_cm}"


def _worker_state(state: dict) -> tuple[bool, str]:
    live = state["live_workers"]
    return not live, f"live workers: {[w.get('worker_id') for w in live]}"


def _unchanged(state: dict) -> tuple[bool, str]:
    ok = not state["new_cells"] and not state["original_cells_changed"]
    return ok, f"new={len(state['new_cells'])} changed={state['original_cells_changed']}"


def check(expectation: str, state: dict, transcript: str, answer: str) -> tuple[bool, str] | None:
    text = expectation.split("] ", 1)[-1]
    new_code = "\n\n".join(state["new_cells"])
    if text.startswith("The first kernel command was the `help(cm)`"):
        return _first_kernel_command_is_help(transcript)
    if text.startswith("Ran the `help(cm)` inspection against the parent before branch.py start"):
        h, s = transcript.find("help(cm)"), transcript.find("branch.py start")
        return (h != -1 and (s == -1 or h < s)), f"help(cm) at {h}, start at {s}"
    if "no cell is in an error state" in text.lower():
        ok = not state["errored_cells"] and ("New cells exist" not in text and "At least one new cell" not in text
                                             or bool(state["new_cells"]))
        return ok, f"errored={state['errored_cells']} new={len(state['new_cells'])}"
    if text.startswith("The notebook file on disk matches the kernel"):
        return state["disk_matches_kernel"], f"disk_matches_kernel={state['disk_matches_kernel']}"
    if text.startswith("None of the original cells were changed"):
        return not state["original_cells_changed"], f"changed={state['original_cells_changed']}"
    if text.startswith("The parent notebook has no new or changed cells"):
        return _unchanged(state)
    if text.startswith("No branch workers are left running"):
        return _worker_state(state)
    if "re-import" in text or "re-importing" in text:
        hits = UNDERSCORE_REIMPORT.findall(new_code)
        return not hits, f"underscore re-imports: {hits}"
    if text.startswith("New cells use named-dimension xarray operations"):
        uses = "session_on_disk" in new_code and re.search(r"\.(mean|isel|sel|groupby)\(", new_code)
        escapes = NUMPY_ESCAPE.findall(new_code)
        return bool(uses) and not escapes, f"xarray ops={bool(uses)} numpy escapes={escapes}"
    if text.startswith("Started workers with `branch.py start`"):
        return bool(re.search(r"branch\.py\s+start[^\n]*--names", transcript)), "branch.py start --names in transcript"
    if text.startswith("The token or token file path was never passed"):
        runs = [line for line in transcript.splitlines() if re.search(r"/temp/\w+/run\b|branch\.py\s+run\b", line)
                and re.match(r"\s*(?:\S+=\S+\s+)*(?:bash|python3?|\$|\(same env\))", line)]
        leaked = [line for line in runs if re.search(r"--token|MARIMO_TOKEN(?:_FILE)?=|parent-token", line)]
        return not leaked, f"run lines mentioning token: {leaked[:3]}"
    if text.startswith("Names m03"):
        return "m03" in answer, "m03 in answer"
    if text.startswith("Names n21"):
        return "n21" in answer, "n21 in answer"
    if text.startswith("Reports the original training accuracy as about 0.816"):
        return bool(re.search(r"0\.81[56]|81\.[56]", answer)), "0.816/81.6% in answer"
    if text.startswith("Reports accuracy with and without the log transform"):
        return len(re.findall(r"0\.82\d*|82\.\d", answer)) >= 2, "two ~0.820 values in answer"
    return None


def grade(run_dir: Path, config: str) -> dict:
    meta = json.loads((run_dir / "eval_metadata.json").read_text())
    outputs = run_dir / config / "outputs"
    transcript = (outputs / "transcript.md").read_text(errors="replace") if (outputs / "transcript.md").exists() else ""
    answer = (outputs / "answer.md").read_text(errors="replace") if (outputs / "answer.md").exists() else ""
    state = harness.inspect(run_dir / "server")
    (run_dir / config / "final_state.json").write_text(json.dumps(state, indent=2))
    expectations = []
    for text in meta["assertions"]:
        result = check(text, state, transcript, answer)
        passed, evidence = result if result else (None, "needs manual grading")
        expectations.append({"text": text, "passed": passed, "evidence": evidence})
    graded = [e for e in expectations if e["passed"] is not None]
    passed = sum(1 for e in graded if e["passed"])
    return {
        "expectations": expectations,
        "summary": {"passed": passed, "failed": len(graded) - passed, "total": len(graded),
                    "pass_rate": round(passed / len(graded), 3) if graded else 0.0,
                    "ungraded": len(expectations) - len(graded)},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--config", default="with_skill")
    args = parser.parse_args()
    result = grade(args.run_dir, args.config)
    (args.run_dir / args.config / "grading.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result["summary"]))


if __name__ == "__main__":
    main()
