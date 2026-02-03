import marimo

__generated_with = "0.10.0"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # Air-Gapped marimo Notebook

        This notebook runs **entirely offline** — all assets (Pyodide, Python
        packages, fonts) are served from the same static site.

        No internet connection required! 🎉
        """
    )
    return


@app.cell
def _():
    import numpy as np
    return (np,)


@app.cell
def _(np):
    # Generate some sample data
    x = np.linspace(0, 4 * np.pi, 200)
    y = np.sin(x) * np.exp(-x / 10)
    return x, y


@app.cell
def _(mo, x, y):
    # Use marimo's built-in plotting (works with WASM)
    import json

    chart_data = [{"x": float(xi), "y": float(yi)} for xi, yi in zip(x, y)]

    mo.md(
        f"""
        ## Damped Sine Wave

        Generated {len(x)} data points using NumPy (running in your browser via
        WebAssembly).

        **x range**: [{float(x[0]):.2f}, {float(x[-1]):.2f}]
        **y range**: [{float(y.min()):.4f}, {float(y.max()):.4f}]
        """
    )
    return (chart_data,)


@app.cell
def _(mo):
    slider = mo.ui.slider(start=1, stop=20, step=1, value=5, label="Frequency multiplier")
    slider
    return (slider,)


@app.cell
def _(mo, np, slider):
    freq = slider.value
    t = np.linspace(0, 2 * np.pi, 100)
    signal = np.sin(freq * t)

    mo.md(
        f"""
        ### Interactive: Frequency = {freq}

        The slider above controls the sine wave frequency.
        This demonstrates that **interactivity works fully offline**.

        - Peak value: {float(signal.max()):.4f}
        - RMS: {float(np.sqrt(np.mean(signal**2))):.4f}
        """
    )
    return


if __name__ == "__main__":
    app.run()
