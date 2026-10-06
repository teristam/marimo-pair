import marimo

__generated_with = "0.25.0"
app = marimo.App()


@app.cell
def _():
    import os
    from pathlib import Path

    from tests.fixtures.branch_models import BranchModel

    _marker_path = Path(os.environ["MARIMO_PAIR_MARKER_PATH"])
    if not _marker_path.exists():
        _marker_path.write_text("started\n", encoding="utf-8")
    records = [1]
    sample = BranchModel("parent", records)
    return BranchModel, records, sample
