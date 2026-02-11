# marimo-offline-pages
<!-- CI test -->

Deploy [marimo](https://marimo.io) WASM notebooks to GitLab Pages (or GitHub
Pages) with **zero internet dependency at runtime**. Everything — Pyodide,
Python packages, fonts — is bundled into the static site.

Built for air-gapped environments where browsers can only access the GitLab
instance.

## Quick start

### Prerequisites

- Python 3.10+
- [marimo](https://marimo.io) (`pip install marimo`)

### Local development

```bash
# Edit notebooks interactively
marimo edit notebooks/example.py

# Build the offline site
python scripts/build.py

# Test locally
cd _site && python -m http.server 8000
# Open http://localhost:8000
```

### Verify offline operation

1. Open the site in Chrome
2. Open DevTools → Network tab
3. Check the "Offline" checkbox
4. Reload the page — it should still work fully

## How it works

The build script (`scripts/build.py`) does the following:

1. **Exports** each notebook in `notebooks/` via `marimo export html-wasm`
2. **Downloads** the full Pyodide distribution (~200MB, includes numpy, pandas,
   scipy, matplotlib, scikit-learn, etc.)
3. **Downloads** Google Fonts (Fira Mono, Lora, PT Sans) as local woff2 files
4. **Downloads** KaTeX CSS and fonts for math rendering
5. **Patches** all CDN URLs in the exported HTML/JS to point to relative local
   paths
6. Outputs a self-contained `_site/` directory

## Adding notebooks

1. Create a new `.py` file in `notebooks/` using `marimo edit notebooks/my_notebook.py`
2. Push to GitHub/GitLab — CI will build and deploy automatically

## Adding Python packages

The Pyodide "full" distribution includes many common packages. If your notebook
needs a package not included:

1. Check if it's in the [Pyodide package list](https://pyodide.org/en/stable/usage/packages-in-pyodide.html)
2. If it's a pure-Python package, download its `.whl` from PyPI
3. Place the wheel in `_site/pyodide/` (or automate in the build script)
4. Update `pyodide-lock.json` with the package entry

## Deploying to GitLab

1. Transfer this repo to your self-hosted GitLab instance
2. The `.gitlab-ci.yml` is already configured
3. Enable GitLab Pages in your project settings
4. Push to `main` — the pipeline will build and deploy

### COOP/COEP headers

For best Pyodide performance (SharedArrayBuffer), configure your GitLab Pages
to serve these headers:

```
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Embedder-Policy: require-corp
```

This may require GitLab admin configuration. Without these headers, Pyodide
still works but uses a slower fallback mode.

## Project structure

```
├── CLAUDE.md               # AI-readable project spec
├── README.md               # This file
├── notebooks/
│   └── example.py          # marimo notebooks
├── public/                  # Data files for notebooks
├── scripts/
│   └── build.py            # Build script (downloads + patches everything)
├── .github/workflows/
│   └── deploy.yml          # GitHub Pages CI
└── .gitlab-ci.yml          # GitLab Pages CI
```

## Limitations

- **Site size**: The full Pyodide distribution is ~200MB. First page load may
  be slow, but browsers cache aggressively.
- **Package support**: Only packages available in Pyodide work (most
  pure-Python packages + selected compiled ones).
- **No multithreading**: WASM notebooks don't support Python threading.
- **2GB memory limit**: Pyodide has a hard memory cap.

## License

MIT
