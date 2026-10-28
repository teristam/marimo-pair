import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import tempfile
    from pathlib import Path

    import marimo as mo
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import seaborn as sns
    import statsmodels.formula.api as smf
    import xarray as xr
    from sklearn.cluster import KMeans
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler

    return (
        FunctionTransformer,
        KMeans,
        LogisticRegression,
        Path,
        Pipeline,
        StandardScaler,
        mo,
        np,
        pd,
        plt,
        smf,
        sns,
        tempfile,
        xr,
    )


@app.cell
def _(mo):
    mo.md("""
    # Trial analysis

    Synthetic behaviour + spiking data shaped like a real session: per-trial
    behaviour in a DataFrame, binned spikes in numpy, labelled rates in xarray,
    then figures and fitted models built on top.
    """)
    return


@app.cell
def _(np, pd):
    rng = np.random.default_rng(0)
    n_trials = 240
    trials = pd.DataFrame(
        {
            "subject": pd.Categorical(rng.choice(["m01", "m02", "m03"], n_trials)),
            "condition": pd.Categorical(
                rng.choice(["easy", "hard"], n_trials), categories=["easy", "hard"], ordered=True
            ),
            "start": pd.Timestamp("2026-01-05 09:00", tz="Europe/London")
            + pd.to_timedelta(np.arange(n_trials) * 7.5, unit="s"),
            "firing_rate": rng.gamma(4.0, 2.5, n_trials),
        }
    )
    trials["rt"] = 0.35 + 0.08 * (trials["condition"] == "hard") - 0.004 * trials["firing_rate"] + rng.normal(0, 0.05, n_trials)
    trials["correct"] = (rng.random(n_trials) < np.where(trials["condition"] == "easy", 0.9, 0.7)).astype(int)
    trials.loc[rng.choice(n_trials, 12, replace=False), "rt"] = np.nan
    trials = trials.set_index(pd.RangeIndex(n_trials, name="trial"))
    trials
    return n_trials, rng, trials


@app.cell
def _(trials):
    summary = trials.groupby(["subject", "condition"], observed=True).agg(
        rt_mean=("rt", "mean"), accuracy=("correct", "mean"), n=("rt", "size")
    )
    summary
    return (summary,)


@app.cell
def _(n_trials, np, rng):
    n_neurons, n_bins = 32, 50
    spikes = rng.poisson(2.0, size=(n_trials, n_neurons, n_bins)).astype(np.int16)
    bad_channels = np.ma.masked_invalid(np.where(rng.random(n_neurons) < 0.1, np.nan, rng.normal(size=n_neurons)))
    probe_layout = np.zeros(n_neurons, dtype=[("channel", "i4"), ("depth_um", "f8"), ("shank", "U2")])
    probe_layout["channel"] = np.arange(n_neurons)
    probe_layout["depth_um"] = np.linspace(0, 3000, n_neurons)
    probe_layout["shank"] = np.where(np.arange(n_neurons) < 16, "A", "B")
    return bad_channels, n_bins, n_neurons, probe_layout, spikes


@app.cell
def _(n_bins, n_neurons, np, spikes, trials, xr):
    rates = xr.DataArray(
        spikes / 0.02,
        dims=("trial", "neuron", "time"),
        coords={
            "trial": trials.index.to_numpy(),
            "neuron": [f"n{i:02d}" for i in range(n_neurons)],
            "time": np.arange(n_bins) * 0.02,
            "condition": ("trial", trials["condition"].astype(str).to_numpy()),
        },
        name="rate",
        attrs={"units": "Hz", "bin_s": 0.02},
    )
    session = xr.Dataset(
        {"rate": rates, "rt": ("trial", trials["rt"].to_numpy())},
        attrs={"subject_ids": "m01,m02,m03", "rig": "rig-2"},
    )
    psth = rates.groupby("condition").mean("trial")
    return psth, rates, session


@app.cell
def _(Path, session, tempfile, xr):
    # Lazily loaded, file-backed Dataset: the way most real sessions are opened.
    _cache_dir = Path(tempfile.mkdtemp(prefix="marimo-pair-analysis-"))
    session.to_netcdf(_cache_dir / "session.nc", engine="scipy")
    session_on_disk = xr.open_dataset(_cache_dir / "session.nc", engine="scipy")
    return (session_on_disk,)


@app.cell
def _(plt, sns, trials):
    fig_rt, ax_rt = plt.subplots(figsize=(5, 3))
    sns.histplot(data=trials, x="rt", hue="condition", ax=ax_rt)
    ax_rt.set_title("Reaction time by condition")
    fig_rt
    return ax_rt, fig_rt


@app.cell
def _(sns, trials):
    rt_grid = sns.relplot(data=trials.reset_index(), x="firing_rate", y="rt", col="subject", hue="condition", height=2.5)
    rt_grid
    return (rt_grid,)


@app.cell
def _(LogisticRegression, Pipeline, StandardScaler, trials):
    _clean = trials.dropna()
    features = _clean[["firing_rate", "rt"]]
    choice_model = Pipeline([("scale", StandardScaler()), ("clf", LogisticRegression())]).fit(
        features, _clean["correct"]
    )
    return choice_model, features


@app.cell
def _(KMeans, spikes):
    neuron_clusters = KMeans(n_clusters=3, n_init=4, random_state=0).fit(spikes.mean(axis=0))
    return (neuron_clusters,)


@app.cell
def _(smf, trials):
    rt_fit = smf.ols("rt ~ C(condition) + firing_rate", data=trials.reset_index()).fit()
    rt_fit.params
    return (rt_fit,)


@app.cell
def _(FunctionTransformer, LogisticRegression, Pipeline, features, np, trials):
    # A cell-defined helper inside a fitted pipeline: common in practice, and
    # not transferable to another kernel because it lives in the notebook.
    def log_rates(x):
        return np.log1p(x)

    custom_model = Pipeline([("log", FunctionTransformer(log_rates)), ("clf", LogisticRegression())]).fit(
        features, trials.dropna()["correct"]
    )
    return custom_model, log_rates


if __name__ == "__main__":
    app.run()
