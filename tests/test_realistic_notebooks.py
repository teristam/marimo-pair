"""Opt-in branch checks against a realistic notebook (tests/notebooks/analysis.py).

The parent and its workers run in the tests/notebooks environment, so values are
real pandas/numpy/xarray/matplotlib/seaborn/sklearn/statsmodels objects. Each
value is fingerprinted with the same code in the parent and in a worker; a
mismatch or a worker-side error means the value did not survive the branch.

Setup: `uv sync --project tests/notebooks`, then
`MARIMO_PAIR_RUN_INTEGRATION=1 python3 -B -m unittest tests.test_realistic_notebooks -v`.
Set MARIMO_PAIR_NOTEBOOK_PYTHON to use another interpreter for the notebook.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_branch_integration import REPO_ROOT, RUN_INTEGRATION, SCRIPTS_DIR, ParentServer, WorkerManager

NOTEBOOKS_DIR = REPO_ROOT / "tests" / "notebooks"
NOTEBOOK_PYTHON = Path(os.environ.get("MARIMO_PAIR_NOTEBOOK_PYTHON") or NOTEBOOKS_DIR / ".venv" / "bin" / "python")

# Each entry is a one-argument lambda, evaluated in both kernels on the named value.
FINGERPRINTS = {
    # Data
    "trials": "lambda v: [int(pd.util.hash_pandas_object(v).sum()), {c: str(t) for c, t in v.dtypes.items()},"
              " str(v['start'].dt.tz), bool(v['condition'].cat.ordered), int(v['rt'].isna().sum())]",
    "summary": "lambda v: [int(pd.util.hash_pandas_object(v).sum()), list(v.index.names), list(v.columns)]",
    "features": "lambda v: [int(pd.util.hash_pandas_object(v).sum()), list(v.shape)]",
    "spikes": "lambda v: [str(v.dtype), list(v.shape), int(v.sum())]",
    "bad_channels": "lambda v: [type(v).__name__, int(v.mask.sum()), round(float(v.sum()), 9)]",
    "probe_layout": "lambda v: [str(v.dtype), v.tolist()]",
    "rates": "lambda v: [dict(v.attrs), list(v.dims), sorted(v.coords), round(float(v.sum()), 3),"
             " v.coords['condition'].values[:5].tolist()]",
    "session": "lambda v: [sorted(v.data_vars), dict(v.attrs), round(float(v['rt'].sum()), 9)]",
    "psth": "lambda v: [list(v.dims), v.coords['condition'].values.tolist(), round(float(v.sum()), 6)]",
    "session_on_disk": "lambda v: [sorted(v.data_vars), dict(v.attrs), round(float(v['rate'].mean()), 6)]",
    # Figures: rendering proves the restored artist tree is usable, not just present.
    "fig_rt": "lambda v: [type(v).__name__, len(v.axes), v.axes[0].get_title(), _png(v) > 1000]",
    "ax_rt": "lambda v: [v.get_title(), len(v.patches), type(v.figure).__name__]",
    "rt_grid": "lambda v: [type(v).__name__, int(v.axes.size), [a.get_title() for a in v.axes.flat], _png(v.figure) > 1000]",
    # Fitted models: predicting proves fitted state and its dependencies came along.
    "choice_model": "lambda v: [list(v.named_steps), round(float(v.predict_proba(globals()['features'])[:, 1].sum()), 9)]",
    "neuron_clusters": "lambda v: [v.labels_.tolist(), round(float(v.cluster_centers_.sum()), 9)]",
    "rt_fit": "lambda v: [{k: round(float(x), 9) for k, x in v.params.items()},"
              " round(float(v.predict(v.model.data.frame.iloc[:5]).sum()), 9)]",
}
DATA_NAMES = ["trials", "summary", "features", "spikes", "bad_channels", "probe_layout",
              "rates", "session", "psth", "session_on_disk"]
LATER_NAMES = ["fig_rt", "ax_rt", "rt_grid", "choice_model", "neuron_clusters", "rt_fit"]


def fingerprint_code(names):
    lines = [
        "import io as _fp_io, json as _fp_json",
        "import numpy as np, pandas as pd",
        "def _png(figure):",
        "    _buffer = _fp_io.BytesIO()",
        "    figure.savefig(_buffer, format='png')",
        "    return len(_buffer.getvalue())",
        "_fp_out = {}",
    ]
    for name in names:
        lines += [
            "try:",
            f"    _fp_out[{name!r}] = ({FINGERPRINTS[name]})(globals()[{name!r}])",
            "except Exception as _fp_error:",
            f"    _fp_out[{name!r}] = 'ERROR ' + type(_fp_error).__name__ + ': ' + str(_fp_error)[:300]",
        ]
    lines.append("print('FINGERPRINT:' + _fp_json.dumps(_fp_out, default=str, sort_keys=True))")
    return "\n".join(lines) + "\n"


def parse_fingerprint(output):
    line = next((line for line in output.splitlines() if line.startswith("FINGERPRINT:")), None)
    if line is None:
        raise AssertionError(f"No fingerprint in output:\n{output[-3000:]}")
    return json.loads(line.partition(":")[2])


@unittest.skipUnless(RUN_INTEGRATION, "set MARIMO_PAIR_RUN_INTEGRATION=1 to run real-server integration tests")
@unittest.skipUnless(NOTEBOOK_PYTHON.exists(), f"run `uv sync --project tests/notebooks` to create {NOTEBOOK_PYTHON}")
class RealisticNotebookBranchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.notebook = cls.root / "analysis.py"
        shutil.copyfile(NOTEBOOKS_DIR / "analysis.py", cls.notebook)
        cls.parent = ParentServer(cls.root, cls.notebook, None, python=str(NOTEBOOK_PYTHON),
                                  ready_names=tuple(FINGERPRINTS) + ("custom_model",))
        try:
            cls.parent.start()
        except BaseException:
            cls.parent.close()
            cls._tmp.cleanup()
            raise
        cls.token_file = cls.root / "parent-token-file"
        cls.token_file.write_text(cls.parent.token + "\n", encoding="utf-8")
        cls.env = {key: value for key, value in os.environ.items() if key not in {"MARIMO_TOKEN", "MARIMO_TOKEN_FILE"}}
        cls.env.update({"MARIMO_PAIR_HOME": str(cls.root / "manager"), "PYTHONDONTWRITEBYTECODE": "1"})
        cls.expected = parse_fingerprint(cls.parent.execute(fingerprint_code(list(FINGERPRINTS))))

    @classmethod
    def tearDownClass(cls):
        try:
            WorkerManager(root_dir=cls.root / "manager").stop_all()
        finally:
            cls.parent.close()
            cls._tmp.cleanup()

    @classmethod
    def cli(cls, *args):
        return subprocess.run(
            [str(NOTEBOOK_PYTHON), "-B", str(SCRIPTS_DIR / "branch.py"), *args], text=True, encoding="utf-8",
            errors="replace", capture_output=True, timeout=300, env=cls.env,
        )

    def start(self, *names):
        started = self.cli("start", "--parent-url", self.parent.url, "--parent-notebook", str(self.notebook),
                           "--token-file", str(self.token_file), "--names", *names)
        return started, (json.loads(started.stdout) if started.returncode == 0 else None)

    def run_in_worker(self, worker, code, *args):
        return subprocess.run(
            [*worker["run_command"], *args, "-"], input=code, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=300, env=self.env,
        )

    def assert_fingerprints_match(self, worker, names):
        ran = self.run_in_worker(worker, fingerprint_code(names))
        self.assertEqual(ran.returncode, 0, (ran.stdout + ran.stderr)[-3000:])
        actual = parse_fingerprint(ran.stdout)
        for name in names:
            with self.subTest(value=name):
                self.assertNotIn("ERROR", str(self.expected[name])[:6], f"parent could not fingerprint {name}")
                self.assertEqual(actual[name], self.expected[name])

    def test_parent_values_survive_start_and_add(self):
        started, worker = self.start(*DATA_NAMES)
        self.assertEqual(started.returncode, 0, started.stderr[-3000:])
        try:
            self.assert_fingerprints_match(worker, DATA_NAMES)
            # Figures and models arrive in a later batch, as a coordinator would add them.
            added = self.cli("add", "--worker", worker["worker_id"], "--token-file", str(self.token_file),
                             "--names", *LATER_NAMES)
            self.assertEqual(added.returncode, 0, added.stderr[-3000:])
            self.assert_fingerprints_match(worker, LATER_NAMES)
            # One batch is one pickle stream: shared references stay shared.
            shared = self.run_in_worker(worker, "print('SAME_FIGURE', ax_rt.figure is fig_rt)")
            self.assertIn("SAME_FIGURE True", shared.stdout, shared.stderr[-2000:])
        finally:
            self.cli("stop", "--worker", worker["worker_id"])

    def test_worker_mutations_and_persistent_cells_do_not_reach_parent(self):
        started, worker = self.start("trials", "rates")
        self.assertEqual(started.returncode, 0, started.stderr[-3000:])
        try:
            mutated = self.run_in_worker(worker, "trials.loc[0, 'rt'] = 999.0\nrates.attrs['units'] = 'kHz'\n")
            self.assertEqual(mutated.returncode, 0, mutated.stderr[-2000:])
            derived = self.run_in_worker(
                worker, "slow = trials[trials['rt'] > 0.5]\nmean_rate = float(rates.mean())\n", "--cell", "derived")
            self.assertEqual(derived.returncode, 0, (derived.stdout + derived.stderr)[-2000:])
            later = self.run_in_worker(worker, "print('ROWS', len(slow) > 0, 'UNITS', rates.attrs['units'])")
            self.assertIn("ROWS True UNITS kHz", later.stdout, later.stderr[-2000:])
            parent_view = self.parent.execute("print('PARENT', trials.loc[0, 'rt'] != 999.0, rates.attrs['units'])")
            self.assertIn("PARENT True Hz", parent_view)
        finally:
            self.cli("stop", "--worker", worker["worker_id"])

    def test_notebook_defined_function_in_model_fails_with_attributable_error(self):
        started, worker = self.start("custom_model")
        if worker is not None:
            self.cli("stop", "--worker", worker["worker_id"])
        self.assertNotEqual(started.returncode, 0, "a pipeline holding a cell-defined function must not branch")
        self.assertIn("custom_model", started.stderr)
        self.assertIn("contains function __main__.log_rates", started.stderr)
        self.assertIn("sklearn.pipeline.Pipeline", started.stderr)
        self.assertNotIn("Traceback", started.stderr)
        listed = self.cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([w for w in json.loads(listed.stdout or "[]") if w.get("status") != "stopped"], [])


if __name__ == "__main__":
    unittest.main()
