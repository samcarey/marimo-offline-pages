# marimo-offline-pages

## Project Goal

Build a fully air-gapped, self-contained marimo WASM notebook deployment for
GitLab Pages. The browser viewing the site has NO internet access — it can only
reach the GitLab instance. All assets (Pyodide runtime, Python packages, fonts,
CSS) must be served from the Pages site itself.

Development happens on GitHub first, then the repo transfers to a self-hosted
GitLab instance.

## Architecture

```
CI/CD (has internet)                   Browser (air-gapped)
────────────────────                   ────────────────────
marimo export html-wasm  ──►  HTML + assets/
download pyodide full    ──►  pyodide/        ──► all served from
download python wheels   ──►  pyodide/        ──► GitLab Pages
download google fonts    ──►  fonts/          ──► (static files)
patch all CDN URLs       ──►  relative paths
```

## Key Technical Problems

1. **Pyodide runtime**: marimo's WASM export loads Pyodide from
   `cdn.jsdelivr.net/pyodide/v{VERSION}/full/`. We must download the full
   Pyodide distribution and rewrite the JS to use a relative `indexURL`.

2. **Python packages**: Packages are installed at runtime via `micropip` from
   PyPI. We must pre-download wheels for all required packages and configure
   micropip/Pyodide to find them locally. The Pyodide "full" distribution
   bundles many common packages (numpy, pandas, scipy, scikit-learn, matplotlib,
   etc.) — for additional pure-Python packages we download their wheels.

3. **Google Fonts**: The export references fonts from `fonts.googleapis.com`
   and `fonts.gstatic.com` (Fira Mono, Lora, PT Sans). Download the font files
   and serve them with a local CSS file.

4. **KaTeX CSS**: Referenced from `cdn.jsdelivr.net/npm/katex@{VERSION}/`.
   Download and bundle locally.

5. **marimo islands JS/CSS** (if using islands mode): Referenced from
   `cdn.jsdelivr.net/npm/@marimo-team/islands@{VERSION}/`. Download and bundle.

## Repository Structure

```
marimo-offline-pages/
├── CLAUDE.md                    # This file (project spec)
├── README.md                    # Human-readable docs
├── notebooks/
│   └── example.py               # Example marimo notebook
├── public/                      # Data files for notebooks
├── scripts/
│   ├── build.py                 # Main build script (orchestrator)
│   ├── patch_exports.py         # Rewrite CDN URLs to local paths
│   └── download_assets.py       # Download pyodide, fonts, packages
├── requirements-build.txt       # Build-time Python dependencies
├── .github/
│   └── workflows/
│       └── deploy.yml           # GitHub Pages deployment
└── .gitlab-ci.yml               # GitLab CI (for later transfer)
```

## Build Process (scripts/build.py)

1. Run `marimo export html-wasm` for each notebook in `notebooks/`
2. Determine the Pyodide version marimo is targeting (inspect the exported JS)
3. Download the full Pyodide release tarball for that version
4. Identify any additional Python packages needed (from notebook imports)
5. Download those wheels from PyPI (Pyodide-compatible)
6. Download Google Fonts (woff2 files) and KaTeX assets
7. Patch all CDN URLs in the exported HTML/JS to point to relative local paths
8. Produce the final `_site/` directory ready for Pages deployment

## URL Patching Strategy

The patch script must handle these URL patterns:
- `https://cdn.jsdelivr.net/pyodide/v{VER}/full/` → `./pyodide/`
- `https://cdn.jsdelivr.net/npm/katex@{VER}/` → `./vendor/katex/`
- `https://fonts.googleapis.com/css2?...` → `./fonts/fonts.css`
- `https://fonts.gstatic.com/...` → `./fonts/...`
- PyPI URLs in micropip → local wheel paths

## Share Link Patching (`patch_wasm_share_links` in build.py)

The "Create WebAssembly link" feature requires two patches for self-hosted
exports:

### Generating links (share-*.js)

marimo's share function hardcodes `baseUrl: "https://marimo.app"` and reads
code via `readCode()` on a SaveWorker (separate Web Worker with its own Pyodide
instance). Two issues:

- **Wrong domain**: The `baseUrl` default is replaced with
  `window.location.href.replace(/#.*/, "")` so links point to the self-hosted
  site.
- **Empty code before worker ready**: If the SaveWorker hasn't loaded yet,
  `readCode()` returns empty. The patched function falls back to decompressing
  the current `#code/…` URL hash (which marimo's `urlFileStore.saveFile()`
  updates on every save). If still empty, it throws an error so the user sees
  an alert instead of getting a broken link.

### Loading shared links (index.html)

marimo's `CompositeFileStore` checks `domElementFileStore` (reads `<marimo-code>`
DOM element) **before** `urlFileStore` (reads `#code/…` hash). Since self-hosted
exports always have `<marimo-code>` embedded in the HTML, the URL hash is
ignored.

Fix: An inline `<script>` removes `<marimo-code>` when `#code/` is present in
the URL hash, letting `urlFileStore` take over.

**Critical placement rule**: The inline script MUST be injected **after**
`</marimo-code>` in the HTML, not before. Inline (non-module) scripts execute
synchronously during HTML parsing and can only see elements that have already
been parsed. Module scripts (`<script type="module">`) are deferred until the
full document is parsed, so the removal happens before marimo's JS runs.

## Important Notes

- The Pyodide "full" distribution is ~200MB (includes all bundled packages).
  Only the packages actually imported get loaded at runtime, so this is fine for
  GitLab Pages but be aware of the artifact size.
- The exported HTML must be served over HTTP (not file://), which GitLab Pages
  handles.
- GitLab Pages needs proper MIME types for `.wasm` files
  (`application/wasm`). Most GitLab instances handle this correctly by default.
- Cross-Origin headers (COOP/COEP) may be needed for SharedArrayBuffer support
  in Pyodide. GitLab Pages may need a `_headers` file or custom nginx config.

## Development Workflow

1. Write/edit notebooks in `notebooks/` using `marimo edit`
2. Test locally: `python scripts/build.py && cd _site && python -m http.server`
3. Push to GitHub → CI builds and deploys to GitHub Pages
4. Later: transfer repo to GitLab, use `.gitlab-ci.yml`

## Testing Air-Gap Simulation

To verify the site works without internet, use browser DevTools:
1. Open the deployed Pages site
2. Open DevTools → Network tab → check "Offline" mode
3. Reload the page — everything should still work
(Note: the initial load must complete first, then toggle offline to verify no
ongoing external fetches are needed.)

Or more rigorously, use a firewall/proxy to block all domains except the
Pages host before the first load.
