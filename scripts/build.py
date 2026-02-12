#!/usr/bin/env python3
"""
Build script for air-gapped marimo WASM notebooks.

Exports marimo notebooks to WASM HTML, downloads all external dependencies
(Pyodide, Python packages, fonts, KaTeX), and patches CDN URLs to relative
local paths. The result is a fully self-contained static site.

Usage:
    python scripts/build.py [--notebooks-dir notebooks] [--output-dir _site]
"""

import argparse
import glob
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import urllib.error
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GOOGLE_FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Fira+Mono:wght@400;500;700&"
    "family=Lora&"
    "family=PT+Sans:wght@400;700&"
    "display=swap"
)

# User-Agent that triggers woff2 responses from Google Fonts
WOFF2_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def run(cmd, **kwargs):
    """Run a shell command, raising on failure."""
    print(f"  → {cmd}")
    result = subprocess.run(cmd, shell=True, check=True, **kwargs)
    return result


def download(url, dest, user_agent=None):
    """Download a URL to a local path."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"  ✓ Already exists: {dest}")
        return

    print(f"  ↓ Downloading: {url}")
    print(f"    → {dest}")
    req = urllib.request.Request(url)
    if user_agent:
        req.add_header("User-Agent", user_agent)
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
        dest.write_bytes(data)
    except urllib.error.HTTPError as e:
        print(f"  ✗ Failed to download {url}: {e}")
        raise


def download_text(url, user_agent=None):
    """Download a URL and return the text content."""
    req = urllib.request.Request(url)
    if user_agent:
        req.add_header("User-Agent", user_agent)
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Step 1: Export notebooks
# ---------------------------------------------------------------------------

def export_notebooks(notebooks_dir, output_dir, mode="run"):
    """Export all marimo notebooks to WASM HTML."""
    print("\n══════════════════════════════════════════")
    print("Step 1: Exporting marimo notebooks")
    print("══════════════════════════════════════════")

    notebooks = sorted(glob.glob(str(Path(notebooks_dir) / "*.py")))
    if not notebooks:
        print(f"  ✗ No .py notebooks found in {notebooks_dir}")
        sys.exit(1)

    single_notebook = len(notebooks) == 1

    for nb in notebooks:
        name = Path(nb).stem
        # Single notebook: export directly to root so it's served at /
        if single_notebook:
            out = Path(output_dir)
        else:
            out = Path(output_dir) / f"{name}"
        print(f"\n  Exporting {nb} → {out}/")
        run(f"marimo export html-wasm {nb} -o {out} --mode {mode} --force")

    return notebooks


# ---------------------------------------------------------------------------
# Step 2: Detect Pyodide version
# ---------------------------------------------------------------------------

def detect_pyodide_version(output_dir):
    """Scan exported JS files to find the Pyodide CDN version being used."""
    print("\n══════════════════════════════════════════")
    print("Step 2: Detecting Pyodide version")
    print("══════════════════════════════════════════")

    # Look for the hardcoded Pyodide version in the worker files
    # Pattern matches: var Io="0.27.7" or similar version strings
    patterns = [
        # Literal CDN URL pattern
        re.compile(r"cdn\.jsdelivr\.net/pyodide/v([0-9]+\.[0-9]+\.[0-9]+)/full"),
        # Version string like Io="0.27.7" (Pyodide version constant)
        re.compile(r'="(0\.[0-9]+\.[0-9]+)"'),
        # pyodide version in loadPyodide context
        re.compile(r'pyodide.*?([0-9]+\.[0-9]+\.[0-9]+)'),
    ]

    # Look specifically in worker files first (they have the Pyodide loading code)
    worker_files = list(Path(output_dir).rglob("*worker*.js"))
    all_js_files = list(Path(output_dir).rglob("*.js"))

    for path in worker_files + all_js_files:
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue

        # First, try to find version in CDN URL pattern
        match = patterns[0].search(text)
        if match:
            version = match.group(1)
            print(f"  ✓ Found Pyodide version: {version} (in {path})")
            return version

        # Look for Pyodide version constant (typically 0.2x.x format)
        # This is more reliable for template literal URLs
        # Check for both double quotes and backticks
        for match in re.finditer(r'["`](0\.2[0-9]+\.[0-9]+)["`]', text):
            version = match.group(1)
            # Verify it looks like a Pyodide version (0.2x.x)
            if version.startswith("0.2"):
                print(f"  ✓ Found Pyodide version: {version} (in {path})")
                return version

    # Fallback: check marimo's installed version info
    print("  ⚠ Could not detect Pyodide version from exports, trying marimo...")
    try:
        result = subprocess.run(
            ["python", "-c", "import marimo; print(marimo.__version__)"],
            capture_output=True, text=True
        )
        print(f"  ℹ marimo version: {result.stdout.strip()}")
    except Exception:
        pass

    print("  ✗ Could not auto-detect Pyodide version.")
    print("    Set PYODIDE_VERSION env var or check the exported JS files.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Step 3: Download Pyodide
# ---------------------------------------------------------------------------

def download_pyodide(version, output_dir):
    """Download the full Pyodide distribution and extract it."""
    print("\n══════════════════════════════════════════")
    print(f"Step 3: Downloading Pyodide {version}")
    print("══════════════════════════════════════════")

    pyodide_dir = Path(output_dir) / "pyodide"

    # Check if already downloaded
    if (pyodide_dir / "pyodide.mjs").exists() or (pyodide_dir / "pyodide.js").exists():
        print(f"  ✓ Pyodide already present at {pyodide_dir}")
        return pyodide_dir

    # Download the full tarball (includes all bundled packages)
    tarball_url = (
        f"https://github.com/pyodide/pyodide/releases/download/"
        f"{version}/pyodide-{version}.tar.bz2"
    )

    tarball_path = Path(output_dir) / f"pyodide-{version}.tar.bz2"
    download(tarball_url, tarball_path)

    # Extract
    print(f"  ⊞ Extracting to {pyodide_dir}...")
    with tarfile.open(tarball_path, "r:bz2") as tar:
        tar.extractall(path=output_dir)

    # The tarball extracts to pyodide-{version}/, rename to pyodide/
    extracted = Path(output_dir) / f"pyodide"
    if not extracted.exists():
        # Try alternate naming
        alt = Path(output_dir) / f"pyodide-{version}"
        if alt.exists():
            alt.rename(extracted)
        else:
            # Some versions extract directly
            for d in Path(output_dir).iterdir():
                if d.is_dir() and d.name.startswith("pyodide"):
                    d.rename(extracted)
                    break

    # Clean up tarball
    tarball_path.unlink(missing_ok=True)

    print(f"  ✓ Pyodide extracted to {pyodide_dir}")
    return pyodide_dir


# ---------------------------------------------------------------------------
# Step 4: Download Google Fonts
# ---------------------------------------------------------------------------

def download_google_fonts(output_dir):
    """Download Google Fonts CSS and all referenced font files.

    Note: Modern marimo exports bundle fonts directly in the assets/ folder,
    so this step is often not needed. We check for bundled fonts first.
    """
    print("\n══════════════════════════════════════════")
    print("Step 4: Checking/Downloading Google Fonts")
    print("══════════════════════════════════════════")

    # Check if fonts are already bundled in the export (marimo >= 0.19 bundles them)
    bundled_fonts = list(Path(output_dir).rglob("assets/*.ttf"))
    if bundled_fonts:
        font_names = [f.stem.split("-")[0] for f in bundled_fonts]
        has_fira = any("FiraMono" in f.name for f in bundled_fonts)
        has_lora = any("Lora" in f.name for f in bundled_fonts)
        has_pt = any("PTSans" in f.name for f in bundled_fonts)
        if has_fira and has_lora and has_pt:
            print(f"  ✓ Fonts already bundled in assets/ ({len(bundled_fonts)} font files)")
            print("    Skipping Google Fonts download (not needed)")
            return None

    fonts_dir = Path(output_dir) / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)

    # Download the CSS with a user-agent that returns woff2 format
    print("  ↓ Fetching font CSS...")
    try:
        css = download_text(GOOGLE_FONTS_CSS_URL, user_agent=WOFF2_USER_AGENT)
    except urllib.error.HTTPError as e:
        print(f"  ⚠ Could not download Google Fonts CSS: {e}")
        print("    This is OK if fonts are already bundled in the export")
        return None

    # Find all font URLs in the CSS
    font_urls = re.findall(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", css)
    print(f"  ℹ Found {len(font_urls)} font files")

    # Download each font file and rewrite the CSS
    for url in font_urls:
        filename = url.split("/")[-1]
        local_path = fonts_dir / filename
        download(url, local_path, user_agent=WOFF2_USER_AGENT)

        # Rewrite URL in CSS to relative path
        css = css.replace(url, f"./{filename}")

    # Write the patched CSS
    css_path = fonts_dir / "fonts.css"
    css_path.write_text(css)
    print(f"  ✓ Font CSS written to {css_path}")

    return fonts_dir


# ---------------------------------------------------------------------------
# Step 5: Download KaTeX
# ---------------------------------------------------------------------------

def download_katex(output_dir):
    """Download KaTeX CSS and font files from jsDelivr/npm.

    Note: Modern marimo exports bundle KaTeX fonts directly in assets/,
    so this step is often not needed. We check for bundled fonts first.
    """
    print("\n══════════════════════════════════════════")
    print("Step 5: Checking/Downloading KaTeX assets")
    print("══════════════════════════════════════════")

    # Check if KaTeX fonts are already bundled in the export
    katex_fonts = list(Path(output_dir).rglob("assets/KaTeX*.ttf"))
    katex_fonts.extend(list(Path(output_dir).rglob("assets/KaTeX*.woff2")))
    if katex_fonts:
        print(f"  ✓ KaTeX fonts already bundled in assets/ ({len(katex_fonts)} files)")
        print("    Skipping KaTeX download (not needed)")
        return None

    # Detect KaTeX version from exports
    katex_version = None
    pattern = re.compile(r"katex@([0-9]+\.[0-9]+\.[0-9]+)")

    for path in Path(output_dir).rglob("*"):
        if path.suffix in (".js", ".html"):
            try:
                text = path.read_text(errors="ignore")
            except Exception:
                continue
            match = pattern.search(text)
            if match:
                katex_version = match.group(1)
                break

    if not katex_version:
        print("  ℹ No KaTeX CDN reference found in exports, skipping")
        return None

    print(f"  ℹ Detected KaTeX version: {katex_version}")

    katex_dir = Path(output_dir) / "vendor" / "katex"
    katex_dir.mkdir(parents=True, exist_ok=True)

    # Download main CSS
    css_url = f"https://cdn.jsdelivr.net/npm/katex@{katex_version}/dist/katex.min.css"
    css_path = katex_dir / "katex.min.css"
    try:
        download(css_url, css_path)
    except urllib.error.HTTPError as e:
        print(f"  ⚠ Could not download KaTeX CSS: {e}")
        return None

    # Parse the CSS for font references and download them
    css = css_path.read_text()
    font_dir = katex_dir / "fonts"
    font_dir.mkdir(parents=True, exist_ok=True)

    font_urls = re.findall(r"url\(([^)]+\.woff2)\)", css)
    for font_rel in font_urls:
        font_name = font_rel.split("/")[-1]
        font_url = f"https://cdn.jsdelivr.net/npm/katex@{katex_version}/dist/{font_rel}"
        download(font_url, font_dir / font_name)
        css = css.replace(font_rel, f"fonts/{font_name}")

    css_path.write_text(css)
    print(f"  ✓ KaTeX assets saved to {katex_dir}")

    return katex_dir


# ---------------------------------------------------------------------------
# Step 6: Patch all CDN URLs
# ---------------------------------------------------------------------------

def patch_cdn_urls(output_dir, pyodide_version, single_notebook=False):
    """Rewrite all CDN URLs in exported files to relative local paths."""
    print("\n══════════════════════════════════════════")
    print("Step 6: Patching CDN URLs to local paths")
    print("══════════════════════════════════════════")

    # We need to handle both literal URLs and JavaScript template literals
    # Template literals use backticks and may have ${...} substitutions
    #
    # Path depth depends on layout:
    #   Multi-notebook:  _site/{name}/assets/worker.js → ../../pyodide/
    #   Single notebook: _site/assets/worker.js        → ../pyodide/
    #
    # Worker/JS files are in assets/, pyodide is at the _site root level.
    # HTML files (index.html) reference fonts/katex relative to their dir.
    if single_notebook:
        pyodide_from_assets = "../pyodide"
        fonts_from_notebook = "./fonts"
        katex_from_notebook = "./vendor/katex"
    else:
        pyodide_from_assets = "../../pyodide"
        fonts_from_notebook = "../fonts"
        katex_from_notebook = "../vendor/katex"

    replacements = [
        # Pyodide lockFileURL template literal → local pyodide-lock.json
        # Matches: lockFileURL:`https://wasm.marimo.app/pyodide-lock.json?v=${e.version}&pyodide=${e.pyodideVersion}`
        (
            re.compile(
                r'lockFileURL:\s*`https://wasm\.marimo\.app/pyodide-lock\.json[^`]*`'
            ),
            f'lockFileURL:`{pyodide_from_assets}/pyodide-lock.json`'
        ),
        # Pyodide indexURL template literal → local pyodide/
        # Matches: indexURL:`https://cdn.jsdelivr.net/pyodide/${e.pyodideVersion}/full/`
        (
            re.compile(
                r'indexURL:\s*`https://cdn\.jsdelivr\.net/pyodide/[^`]*`'
            ),
            f'indexURL:`{pyodide_from_assets}/`'
        ),
        # setCdnUrl call → remove or make no-op (we're loading locally)
        # Matches: s.setCdnUrl(`https://cdn.jsdelivr.net/pyodide/v${d.version}/full/`)
        (
            re.compile(
                r'\.setCdnUrl\(`https://cdn\.jsdelivr\.net/pyodide/[^`]*`\)'
            ),
            f'.setCdnUrl(`{pyodide_from_assets}/`)'
        ),
        # Pyodide CDN literal URLs (with version) → local pyodide/
        (
            f"https://cdn.jsdelivr.net/pyodide/v{pyodide_version}/full/",
            f"{pyodide_from_assets}/"
        ),
        (
            f"https://cdn.jsdelivr.net/pyodide/v{pyodide_version}/full",
            f"{pyodide_from_assets}"
        ),
        # Pyodide CDN generic pattern for any version
        (
            re.compile(
                r'https://cdn\.jsdelivr\.net/pyodide/v[0-9.]+/full/'
            ),
            f"{pyodide_from_assets}/"
        ),
        # Google Fonts CSS → local fonts/fonts.css
        (
            re.compile(
                r'https://fonts\.googleapis\.com/css2\?'
                r'family=Fira\+Mono[^"\'>\s]*'
            ),
            f"{fonts_from_notebook}/fonts.css"
        ),
        # Google Fonts preconnect hints (remove or replace)
        (
            "https://fonts.googleapis.com",
            ""
        ),
        (
            "https://fonts.gstatic.com",
            ""
        ),
        # KaTeX CDN → local vendor/katex/
        (
            re.compile(
                r'https://cdn\.jsdelivr\.net/npm/katex@[0-9.]+/dist/'
            ),
            f"{katex_from_notebook}/"
        ),
        # unpkg.com emoji/icons data → will be downloaded separately
        # For now, these are optional features that fail gracefully

        # Share "Create WebAssembly Link" → use current page URL instead of marimo.app
        # Matches: baseUrl:C="https://marimo.app" (default param in destructuring)
        # Replace the default so the share link points to the current deployment.
        (
            re.compile(
                r'''(baseUrl:\w+=)"https://marimo\.app"'''
            ),
            r'\1window.location.href.replace(/#.*/,"")'
        ),
    ]

    patched_count = 0
    for path in Path(output_dir).rglob("*"):
        if path.suffix not in (".js", ".html", ".mjs", ".css"):
            continue
        if "pyodide" in str(path) or "vendor" in str(path) or "fonts" in str(path):
            continue  # Don't patch downloaded vendor files

        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue

        original = text
        for old, new in replacements:
            if isinstance(old, re.Pattern):
                text = old.sub(new, text)
            else:
                text = text.replace(old, new)

        if text != original:
            path.write_text(text)
            patched_count += 1
            print(f"  ✓ Patched: {path}")

    print(f"  ✓ Patched {patched_count} files total")


def patch_publish_button(output_dir):
    """Remove the 'Publish HTML to web' menu item from the notebook UI.

    This item posts the notebook's HTML to static.marimo.app, which is
    undesirable for air-gapped / sensitive deployments.  We force the menu
    item's ``hidden`` flag to ``true`` so it never appears.
    """
    print("\n══════════════════════════════════════════")
    print("Step 6a-0: Removing 'Publish HTML to web' button")
    print("══════════════════════════════════════════")

    patched = 0
    for path in Path(output_dir).rglob("useNotebookActions-*.js"):
        text = path.read_text(errors="ignore")
        # In the minified JS the menu item looks like:
        #   {icon:V,label:"Publish HTML to web",hidden:K,handle:_}
        #   {icon:V,label:"Publish HTML to web",hidden:!H,handle:_}
        # Replace `hidden:<expr>` with `hidden:!0` (always hidden).
        # The value may be a variable (K), negated variable (!H), etc.
        new_text = re.sub(
            r'(label:"Publish HTML to web",hidden:)[^,]+',
            r'\g<1>!0',
            text,
        )
        if new_text != text:
            path.write_text(new_text)
            patched += 1
            print(f"  ✓ Removed publish button: {path}")
        else:
            print(f"  ⚠ 'Publish HTML to web' pattern not found in {path}")

    if patched == 0:
        # Try broader search in case the chunk name changed
        for path in Path(output_dir).rglob("*.js"):
            text = path.read_text(errors="ignore")
            if "Publish HTML to web" not in text:
                continue
            new_text = re.sub(
                r'(label:"Publish HTML to web",hidden:)[^,]+',
                r'\g<1>!0',
                text,
            )
            if new_text != text:
                path.write_text(new_text)
                patched += 1
                print(f"  ✓ Removed publish button: {path}")

    print(f"  ✓ Patched {patched} files")


def patch_mode_url_sync(output_dir):
    """Patch mode-*.js to sync view mode state into the URL.

    When the user toggles between edit and present (app) mode, this patch
    updates ``?view-as=present`` in the URL via ``history.replaceState``.
    The share function already reads ``window.location.href``, so the query
    parameter is automatically included in generated share links.
    """
    print("\n══════════════════════════════════════════")
    print("Step 6a-1: Syncing view mode → URL")
    print("══════════════════════════════════════════")

    patched = 0
    for path in Path(output_dir).rglob("mode-*.js"):
        text = path.read_text(errors="ignore")

        # Identify the jotai store import variable.
        # Pattern: import{i as <store>,p as <atom_creator>}from"./useEvent-*.js"
        store_match = re.search(
            r'import\{i as (\w+),', text
        )
        if not store_match:
            print(f"  ⚠ WARNING: Could not find store import in {path}")
            continue
        store = store_match.group(1)

        # Identify the mode atom variable.
        # It's the first `const <var>=<atom>({mode:...,"not-set"...cellAnchor` pattern.
        atom_match = re.search(
            r'const (\w+)=\w+\(\{mode:', text
        )
        if not atom_match:
            print(f"  ⚠ WARNING: Could not find mode atom in {path}")
            continue
        mode_atom = atom_match.group(1)

        # Inject subscription before the final `export{`
        subscription = (
            f'{store}.sub({mode_atom},()=>{{var _m={store}.get({mode_atom}).mode;'
            f'var _u=new URL(window.location.href);'
            f'if(_m==="present")_u.searchParams.set("view-as","present");'
            f'else _u.searchParams.delete("view-as");'
            f'if(_u.href!==window.location.href)history.replaceState(null,"",_u.href)}});'
        )

        export_match = re.search(r'export\{', text)
        if not export_match:
            print(f"  ⚠ WARNING: Could not find export{{ in {path}")
            continue

        insert_pos = export_match.start()
        text = text[:insert_pos] + subscription + text[insert_pos:]
        path.write_text(text)
        patched += 1
        print(f"  ✓ Patched mode URL sync: {path}")
        print(f"    store={store}, modeAtom={mode_atom}")

    if patched == 0:
        print("  ⚠ WARNING: No mode-*.js files were patched")
    print(f"  ✓ Patched {patched} files for mode URL sync")


def patch_layout_url_sync(output_dir):
    """Patch layout-*.js to sync layout state ↔ URL.

    Two patches:
    1. **Read URL on load**: Replace the default ``selectedLayout:"vertical"``
       with a dynamic read from ``?layout=`` so that shared links open in the
       correct layout (slides, grid, etc.).
    2. **Write URL on change**: Subscribe to layout atom changes and update
       ``?layout=<type>`` in the URL via ``history.replaceState``.
    """
    print("\n══════════════════════════════════════════")
    print("Step 6a-2: Syncing layout ↔ URL")
    print("══════════════════════════════════════════")

    patched = 0
    for path in Path(output_dir).rglob("layout-*.js"):
        text = path.read_text(errors="ignore")

        # --- 2a: Read ?layout= from URL at initialization ---
        default_pat = 'selectedLayout:"vertical"'
        if default_pat in text:
            text = text.replace(
                default_pat,
                'selectedLayout:(new URL(window.location.href).searchParams.get("layout")||"vertical")',
            )
            print(f"  ✓ Patched layout default from URL: {path}")
        else:
            print(f"  ⚠ WARNING: Could not find selectedLayout:\"vertical\" in {path}")
            continue

        # --- 2b: Sync layout changes → URL ---

        # Store import (same pattern as mode-*.js)
        store_match = re.search(r'import\{i as (\w+),', text)
        if not store_match:
            print(f"  ⚠ WARNING: Could not find store import in {path}")
            continue
        store = store_match.group(1)

        # Promise variable: <var>=Promise.all
        promise_match = re.search(r'(\w+)=Promise\.all', text)
        if not promise_match:
            print(f"  ⚠ WARNING: Could not find Promise.all in {path}")
            continue
        promise_var = promise_match.group(1)

        # Layout atom: valueAtom:<var> inside the layout factory
        layout_atom_match = re.search(r'valueAtom:(\w+)', text)
        if not layout_atom_match:
            print(f"  ⚠ WARNING: Could not find valueAtom:<var> in {path}")
            continue
        layout_atom = layout_atom_match.group(1)

        # Inject subscription before `export{`
        subscription = (
            f'{promise_var}.then(()=>{{{store}.sub({layout_atom},()=>{{'
            f'var _l={store}.get({layout_atom}).selectedLayout;'
            f'var _u=new URL(window.location.href);'
            f'if(_l&&_l!=="vertical")_u.searchParams.set("layout",_l);'
            f'else _u.searchParams.delete("layout");'
            f'if(_u.href!==window.location.href)history.replaceState(null,"",_u.href)}})}})'
            f';'
        )

        export_match = re.search(r'export\{', text)
        if not export_match:
            print(f"  ⚠ WARNING: Could not find export{{ in {path}")
            continue

        insert_pos = export_match.start()
        text = text[:insert_pos] + subscription + text[insert_pos:]
        path.write_text(text)
        patched += 1
        print(f"  ✓ Patched layout URL sync: {path}")
        print(f"    store={store}, promise={promise_var}, layoutAtom={layout_atom}")

    if patched == 0:
        print("  ⚠ WARNING: No layout-*.js files were patched")
    print(f"  ✓ Patched {patched} files for layout URL sync")


def patch_wasm_share_links(output_dir, single_notebook=False):
    """Patch the exported WASM notebooks so that 'Create WebAssembly link' works.

    Two problems exist in self-hosted marimo WASM exports:

    1. **Generating share links**: The 'Create WebAssembly link' button calls
       readCode() on a SaveWorker (a separate Web Worker with its own Pyodide
       instance).  If that worker hasn't fully loaded yet, readCode() returns
       empty and the share URL has no #code/… fragment.  We patch the share
       function to fall back to reading from the <marimo-code> DOM element.

    2. **Loading share links**: marimo's file-store chain checks the
       <marimo-code> DOM element *before* the URL hash.  For self-hosted
       exports the element always exists, so #code/… is ignored.  We inject a
       small inline script that removes <marimo-code> when a #code/ hash is
       present, letting marimo's urlFileStore take over.
    """
    print("\n══════════════════════════════════════════")
    print("Step 6a: Patching WASM share link support")
    print("══════════════════════════════════════════")

    patched = 0

    # --- Part 1: Patch share-*.js -----------------------------------------
    #
    # The share function looks like (minified):
    #   function X(w){let{code:y,baseUrl:C="https://marimo.app"}=w,
    #     g=new URL(C);return y&&(g.hash=`#code/${...}`),g.href}
    #
    # Three fixes:
    #  a) Replace the hardcoded baseUrl default ("https://marimo.app") with
    #     the current page URL, so the link points to THIS self-hosted site.
    #  b) When readCode() returns empty (save worker not ready yet), try the
    #     URL hash as a fallback (it's updated on every save by marimo's
    #     urlFileStore.saveFile).  This gives the last-saved version.
    #  c) If STILL no code available, throw an error so the caller's
    #     clipboard-copy and "Copied" toast are skipped.  The user sees an
    #     alert telling them to wait.
    for path in Path(output_dir).rglob("share-*.js"):
        text = path.read_text(errors="ignore")

        # (a) Fix baseUrl default to use the current page URL
        text = re.sub(
            r'(baseUrl:\w+=)"https://marimo\.app"',
            r'\1window.location.href.replace(/#.*/,"")',
            text,
        )

        # (b)+(c) Inject URL-hash fallback + error before the return stmt.
        # Group 2 captures the minified variable name for "code".
        # Group 3 captures the LZ-String module alias used for compress.
        share_re = re.compile(
            r'(function \w+\(\w+\)\{let\{code:(\w+),baseUrl:\w+='
            r'[^}]+\}=\w+,\w+=new URL\(\w+\);)'
            r'(return )'
        )
        match = share_re.search(text)
        if match:
            code_var = match.group(2)

            # Find the LZ-String module alias.  The original code uses
            # (0,E.compressToEncodedURIComponent)(y) — we need the same
            # alias (E, P, etc.) for decompressFromEncodedURIComponent.
            lz_match = re.search(
                r'\(0,(\w+)\.compressToEncodedURIComponent\)',
                text[match.end():]
            )
            lz_alias = lz_match.group(1) if lz_match else None

            parts = []
            # Fallback to URL hash (last saved version)
            if lz_alias:
                parts.append(
                    f'if(!{code_var}){{'
                    f'var _h=window.location.hash;'
                    f'if(_h&&_h.indexOf("#code/")===0)'
                    f'{code_var}=(0,{lz_alias}.decompressFromEncodedURIComponent)'
                    f'(_h.slice(6))}}'
                )
            # Fallback to <marimo-code> DOM element (original notebook code).
            # On first load before any save, the URL hash is empty and the
            # save worker hasn't initialised yet, but <marimo-code> always
            # has the notebook code (URL-encoded).  decodeURIComponent gives
            # us the raw Python source, which the share function then
            # compresses into the #code/… hash fragment.
            parts.append(
                f'if(!{code_var}){{'
                f'var _el=document.querySelector("marimo-code");'
                f'if(_el){code_var}=decodeURIComponent(_el.textContent||"").trim()}}'
            )
            # If still no code, refuse — don't silently give a broken URL
            parts.append(
                f'if(!{code_var}){{throw new Error('
                f'"Notebook still loading. Please wait and try again.")}}'
            )

            fallback = ''.join(parts)
            insert_pos = match.end() - len(match.group(3))
            text = text[:insert_pos] + fallback + text[insert_pos:]
            path.write_text(text)
            patched += 1
            print(f"  ✓ Patched share function: {path}")
        else:
            print(f"  ⚠ Could not find share function pattern in {path}")

    # --- Part 2: Inject URL-hash handler into each notebook index.html ---
    #
    # When someone opens a URL with #code/…, we remove the <marimo-code>
    # element so that marimo's urlFileStore reads from the hash instead.
    #
    # IMPORTANT: This script MUST appear AFTER <marimo-code> in the HTML.
    # Inline (non-module) scripts execute synchronously during parsing, so
    # they can only see DOM elements that have already been parsed.  If the
    # script were placed before <marimo-code>, querySelector would return
    # null.  Placing it right after </marimo-code> guarantees the element
    # exists in the DOM when the script runs, and it still executes before
    # the deferred module scripts that initialize marimo's file stores.
    hash_handler_script = """\n<script data-marimo-share="true">
    (function(){
      // --- Receiving side: handle incoming #code/… share links ---
      // marimo's file-store checks <marimo-code> before the URL hash, so
      // we remove the element when a hash is present to let the hash win.
      var h=window.location.hash;
      if(h&&h.indexOf("#code/")===0){
        var el=document.querySelector("marimo-code");
        if(el)el.remove();
      }
      // --- Show a user-friendly message when share fails ---
      // The patched share function throws when code isn't ready yet.
      // Catch the unhandled rejection and surface it as an alert.
      window.addEventListener("unhandledrejection",function(ev){
        if(ev.reason&&/Notebook still loading/.test(ev.reason.message)){
          ev.preventDefault();
          alert(ev.reason.message);
        }
      });
    })();
    </script>"""

    if single_notebook:
        # Single notebook: the root index.html IS the notebook
        notebook_htmls = [Path(output_dir) / "index.html"]
    else:
        # Multi notebook: each notebook is in a subdirectory
        notebook_htmls = [
            p for p in Path(output_dir).rglob("*/index.html")
            if p.parent != Path(output_dir)
        ]

    for path in notebook_htmls:

        text = path.read_text(errors="ignore")

        # Already patched?
        if 'data-marimo-share' in text:
            continue

        # Insert AFTER </marimo-code> — the element must already be in the
        # DOM when this inline script runs during HTML parsing.
        insert_re = re.compile(r'(</marimo-code>)')
        m = insert_re.search(text)
        if m:
            text = text[:m.end()] + hash_handler_script + text[m.end():]
            path.write_text(text)
            patched += 1
            print(f"  ✓ Injected URL-hash handler: {path}")
        else:
            print(f"  ⚠ Could not find </marimo-code> in {path}")

    print(f"  ✓ Patched {patched} files for share-link support")


# ---------------------------------------------------------------------------
# Step 7: Handle PyPI/micropip for additional packages
# ---------------------------------------------------------------------------

def download_marimo_base(output_dir, marimo_version):
    """Download the marimo-base wheel for WASM support."""
    print("\n══════════════════════════════════════════")
    print(f"Step 6b: Downloading marimo-base {marimo_version}")
    print("══════════════════════════════════════════")

    pyodide_dir = Path(output_dir) / "pyodide"
    pyodide_lock = pyodide_dir / "pyodide-lock.json"

    # Check if marimo-base already exists
    existing = list(pyodide_dir.glob("marimo_base*.whl"))
    if existing:
        print(f"  ✓ marimo-base already present: {existing[0].name}")
        return

    # Download marimo-base wheel
    wheel_name = f"marimo_base-{marimo_version}-py3-none-any.whl"
    wheel_url = f"https://files.pythonhosted.org/packages/py3/m/marimo-base/{wheel_name}"

    # Try direct PyPI download
    try:
        download(wheel_url, pyodide_dir / wheel_name)
    except Exception:
        # Fallback: use pip to download
        print(f"  ↓ Downloading via pip...")
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = subprocess.run(
                    ["pip", "download", "--no-deps", "--only-binary=:all:",
                     f"--dest={tmpdir}", f"marimo-base=={marimo_version}"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    # Find the downloaded wheel
                    for f in Path(tmpdir).glob("*.whl"):
                        shutil.copy(f, pyodide_dir / f.name)
                        print(f"  ✓ Downloaded: {f.name}")
                        break
                else:
                    print(f"  ⚠ Could not download marimo-base: {result.stderr}")
                    return
        except Exception as e:
            print(f"  ⚠ Could not download marimo-base: {e}")
            return

    # Update pyodide-lock.json to include marimo-base
    if pyodide_lock.exists():
        try:
            import hashlib
            lock_data = json.loads(pyodide_lock.read_text())
            wheel_path = list(pyodide_dir.glob("marimo_base*.whl"))[0]

            # Compute sha256
            wheel_sha = hashlib.sha256(wheel_path.read_bytes()).hexdigest()

            # Add marimo-base entry to packages
            if "packages" in lock_data and "marimo-base" not in lock_data["packages"]:
                lock_data["packages"]["marimo-base"] = {
                    "name": "marimo-base",
                    "version": marimo_version,
                    "file_name": wheel_path.name,
                    "install_dir": "site",
                    "sha256": wheel_sha,
                    "package_type": "package",
                    "depends": [],  # marimo-base has minimal deps for WASM
                    "imports": ["marimo"]
                }
                pyodide_lock.write_text(json.dumps(lock_data, indent=2))
                print(f"  ✓ Updated pyodide-lock.json with marimo-base")
        except Exception as e:
            print(f"  ⚠ Could not update pyodide-lock.json: {e}")


def download_extra_package(output_dir, package_name, imports=None, min_version=None):
    """Download an extra pure-Python package from PyPI and add to pyodide-lock.json.

    If the package exists in Pyodide but is older than min_version, it will be upgraded.
    """
    pyodide_dir = Path(output_dir) / "pyodide"
    pyodide_lock = pyodide_dir / "pyodide-lock.json"

    # Normalize package name for filesystem (underscores)
    pkg_normalized = package_name.lower().replace("-", "_")
    pkg_key = package_name.lower()

    # Check current version in pyodide-lock.json
    current_version = None
    if pyodide_lock.exists():
        lock_data = json.loads(pyodide_lock.read_text())
        if pkg_key in lock_data.get("packages", {}):
            current_version = lock_data["packages"][pkg_key].get("version")

    # Fetch package info from PyPI
    try:
        pypi_url = f"https://pypi.org/pypi/{package_name}/json"
        req = urllib.request.Request(pypi_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        latest_version = data["info"]["version"]
        urls = data["releases"].get(latest_version, [])

        # Find py3-none-any wheel
        wheel_info = None
        for u in urls:
            if u["filename"].endswith("-py3-none-any.whl"):
                wheel_info = u
                break

        if not wheel_info:
            print(f"  ⚠ No pure Python wheel found for {package_name}")
            return False

        # Check if we need to download (new package or upgrade needed)
        need_download = False
        if current_version is None:
            need_download = True
        elif current_version != latest_version:
            # Compare versions - download if PyPI has newer
            from packaging.version import Version
            if Version(latest_version) > Version(current_version):
                print(f"  ↑ Upgrading {package_name}: {current_version} → {latest_version}")
                need_download = True
                # Remove old wheel files
                for old_whl in pyodide_dir.glob(f"{pkg_normalized}*.whl"):
                    old_whl.unlink()
            else:
                print(f"  ✓ {package_name} {current_version} already up to date")
                return True
        else:
            print(f"  ✓ {package_name} {current_version} already present")
            return True

        wheel_url = wheel_info["url"]
        wheel_name = wheel_info["filename"]
        wheel_sha = wheel_info.get("digests", {}).get("sha256", "")

        print(f"  ↓ Downloading {package_name} {latest_version}")
        download(wheel_url, pyodide_dir / wheel_name)

        # Update pyodide-lock.json (add or update entry)
        if pyodide_lock.exists():
            lock_data = json.loads(pyodide_lock.read_text())
            if "packages" in lock_data:
                lock_data["packages"][pkg_key] = {
                    "name": pkg_key,
                    "version": latest_version,
                    "file_name": wheel_name,
                    "install_dir": "site",
                    "sha256": wheel_sha,
                    "package_type": "package",
                    "depends": [],
                    "imports": imports or [pkg_normalized]
                }
                pyodide_lock.write_text(json.dumps(lock_data, indent=2))
                if current_version:
                    print(f"  ✓ Updated {package_name} in pyodide-lock.json")
                else:
                    print(f"  ✓ Added {package_name} to pyodide-lock.json")
        return True

    except Exception as e:
        print(f"  ⚠ Failed to download {package_name}: {e}")
        return False


def download_extra_packages(output_dir):
    """Download additional packages required by marimo that aren't in Pyodide."""
    print("\n══════════════════════════════════════════")
    print("Step 6c: Downloading extra packages")
    print("══════════════════════════════════════════")

    # Packages marimo needs that aren't in the Pyodide full distribution
    # or need newer versions than Pyodide provides
    extra_packages = [
        # Core marimo dependencies not in Pyodide
        ("Markdown", ["markdown"]),
        ("pymdown-extensions", ["pymdownx"]),
        ("tomlkit", ["tomlkit"]),
        ("itsdangerous", ["itsdangerous"]),
        # narwhals 2.x needed (Pyodide has 1.41, marimo needs >=2.0 for stable.v2)
        ("narwhals", ["narwhals"]),
    ]

    for pkg_name, imports in extra_packages:
        download_extra_package(output_dir, pkg_name, imports)


def patch_micropip_for_offline(output_dir):
    """
    Patch micropip configuration so packages are loaded from the local
    Pyodide distribution rather than fetching from PyPI.

    The Pyodide 'full' distribution includes most scientific packages.
    For additional packages, wheels should be placed in the pyodide/ directory
    and the pyodide-lock.json should be updated.
    """
    print("\n══════════════════════════════════════════")
    print("Step 7: Configuring offline package loading")
    print("══════════════════════════════════════════")

    pyodide_lock = Path(output_dir) / "pyodide" / "pyodide-lock.json"
    if pyodide_lock.exists():
        print(f"  ✓ pyodide-lock.json present — bundled packages will load locally")
    else:
        print(f"  ⚠ pyodide-lock.json not found — package loading may fail offline")

    # Check if marimo itself is available as a wheel or needs to be added
    # marimo is typically loaded by the WASM export's own assets, not via micropip
    print("  ℹ The Pyodide 'full' distribution includes numpy, scipy, pandas,")
    print("    matplotlib, scikit-learn, and many more. If your notebook imports")
    print("    a package NOT in the Pyodide distribution, you'll need to:")
    print("    1. Download its .whl file (pure Python wheel)")
    print("    2. Place it in _site/pyodide/")
    print("    3. Add an entry to pyodide-lock.json")
    print("    See: https://pyodide.org/en/stable/usage/loading-packages.html")


# ---------------------------------------------------------------------------
# Step 8: Create index page
# ---------------------------------------------------------------------------

def create_index_page(output_dir, notebooks):
    """Create an index.html listing all notebooks."""
    print("\n══════════════════════════════════════════")
    print("Step 8: Creating index page")
    print("══════════════════════════════════════════")

    if len(notebooks) <= 1:
        print("  ℹ Single notebook — index.html already points to it")
        return

    names = [Path(nb).stem for nb in notebooks]
    links = "\n".join(
        f'        <li><a href="{name}/index.html">{name}</a></li>'
        for name in names
    )

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>marimo Notebooks</title>
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 600px; margin: 2rem auto; padding: 0 1rem; }}
        a {{ color: #2563eb; }}
        li {{ margin: 0.5rem 0; }}
    </style>
</head>
<body>
    <h1>marimo Notebooks</h1>
    <ul>
{links}
    </ul>
    <p><em>Fully offline — all assets served locally.</em></p>
</body>
</html>
"""
    index_path = Path(output_dir) / "index.html"
    index_path.write_text(html)
    print(f"  ✓ Index page created at {index_path}")


# ---------------------------------------------------------------------------
# Step 9: Add .nojekyll and headers
# ---------------------------------------------------------------------------

def add_metadata_files(output_dir):
    """Add .nojekyll (GitHub) and _headers (optional COOP/COEP)."""
    print("\n══════════════════════════════════════════")
    print("Step 9: Adding metadata files")
    print("══════════════════════════════════════════")

    # .nojekyll — prevents GitHub Pages from ignoring underscore-prefixed dirs
    nojekyll = Path(output_dir) / ".nojekyll"
    nojekyll.touch()
    print(f"  ✓ Created {nojekyll}")

    # _headers — for Pyodide SharedArrayBuffer support (Netlify/Cloudflare
    # format, also useful as documentation for GitLab nginx config)
    headers = Path(output_dir) / "_headers"
    headers.write_text("""\
/*
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
""")
    print(f"  ✓ Created {headers} (COOP/COEP for SharedArrayBuffer)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build air-gapped marimo WASM notebooks for static hosting"
    )
    parser.add_argument(
        "--notebooks-dir", default="notebooks",
        help="Directory containing marimo .py notebooks (default: notebooks)"
    )
    parser.add_argument(
        "--output-dir", default="_site",
        help="Output directory for the static site (default: _site)"
    )
    parser.add_argument(
        "--mode", default="run", choices=["run", "edit"],
        help="Export mode: 'run' for readonly, 'edit' for editable (default: run)"
    )
    parser.add_argument(
        "--pyodide-version", default=None,
        help="Override Pyodide version (auto-detected from exports by default)"
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    # Clean output directory
    if output_dir.exists():
        print(f"Cleaning {output_dir}...")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # Step 1: Export notebooks
    notebooks = export_notebooks(args.notebooks_dir, output_dir, args.mode)

    # Step 2: Detect Pyodide version
    pyodide_version = args.pyodide_version or os.environ.get("PYODIDE_VERSION")
    if not pyodide_version:
        pyodide_version = detect_pyodide_version(output_dir)

    # Step 3: Download Pyodide
    download_pyodide(pyodide_version, output_dir)

    # Step 4: Download Google Fonts
    download_google_fonts(output_dir)

    # Step 5: Download KaTeX
    download_katex(output_dir)

    # Step 6: Patch CDN URLs
    single_notebook = len(notebooks) == 1
    patch_cdn_urls(output_dir, pyodide_version, single_notebook=single_notebook)

    # Step 6a-0: Remove 'Publish HTML to web' button
    patch_publish_button(output_dir)

    # Step 6a-1/2: Sync view mode and layout to URL for share links
    patch_mode_url_sync(output_dir)
    patch_layout_url_sync(output_dir)

    # Step 6a: Patch WASM share link support
    patch_wasm_share_links(output_dir, single_notebook=single_notebook)

    # Step 6b: Download marimo-base for WASM support
    try:
        result = subprocess.run(
            ["python", "-c", "import marimo; print(marimo.__version__)"],
            capture_output=True, text=True
        )
        marimo_version = result.stdout.strip() if result.returncode == 0 else "0.19.7"
    except Exception:
        marimo_version = "0.19.7"  # fallback
    download_marimo_base(output_dir, marimo_version)

    # Step 6c: Download extra packages (Markdown, etc.)
    download_extra_packages(output_dir)

    # Step 7: Configure offline packages
    patch_micropip_for_offline(output_dir)

    # Step 8: Create index page
    create_index_page(output_dir, notebooks)

    # Step 9: Metadata files
    add_metadata_files(output_dir)

    # Summary
    site_size = sum(
        f.stat().st_size for f in output_dir.rglob("*") if f.is_file()
    )
    print("\n══════════════════════════════════════════")
    print("Build complete!")
    print("══════════════════════════════════════════")
    print(f"  Output: {output_dir}/")
    print(f"  Size:   {site_size / (1024*1024):.1f} MB")
    print(f"  Pyodide version: {pyodide_version}")
    print(f"  Notebooks: {len(notebooks)}")
    print()
    print("  To test locally:")
    print(f"    cd {output_dir} && python -m http.server 8000")
    print("    Then open http://localhost:8000")
    print()
    print("  To verify offline operation:")
    print("    Open DevTools → Network → check 'Offline' → reload")


if __name__ == "__main__":
    main()
