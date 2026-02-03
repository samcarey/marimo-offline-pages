# Data files

Place data files (CSV, JSON, images, etc.) in this directory.

Notebooks can access them via:

```python
import marimo as mo
path = mo.notebook_location() / "public" / "data.csv"
```

These files will be bundled into the WASM export.
