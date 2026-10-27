import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import seaborn as sns


    return (sns,)


@app.cell
def _(sns):
    df = sns.load_dataset('iris')
    return (df,)


@app.cell
def _(df):
    df
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
