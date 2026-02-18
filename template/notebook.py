import marimo

__generated_with = "0.19.11"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md("# My Analysis")
    return


@app.cell
def _(mo):
    import pathlib

    data_dir = pathlib.Path("data")
    if data_dir.exists():
        files = sorted(data_dir.rglob("*"))
        mo.md(
            "**Data files found:**\n\n"
            + "\n".join(f"- `{f}`" for f in files if f.is_file())
        )
    else:
        mo.md("_No `data/` directory found._")
    return


if __name__ == "__main__":
    app.run()
