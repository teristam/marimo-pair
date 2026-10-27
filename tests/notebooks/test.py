import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import seaborn as sns


    return (sns,)


@app.cell
def _(sns):
    df_iris = sns.load_dataset('iris')
    df_crashes = sns.load_dataset('car_crashes')
    df_mri = sns.load_dataset('fmri')
    return (df_crashes,)


@app.cell
def _(df):
    df
    return


@app.cell
def _(df_crashes):
    df_crashes
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
