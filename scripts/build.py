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
import configparser
import glob
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.error
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Pin the marimo version used for exports.  Bump this deliberately after
# verifying patches still apply (see scripts/check_upgrade.sh).
MARIMO_VERSION = "0.19.11"

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
# Patch-error tracking
# ---------------------------------------------------------------------------
# Every patch function records failures here instead of printing warnings.
# After all patches run, check_patch_errors() aborts the build with a
# summary if anything failed — no more silent breakage.

_patch_errors: list[tuple[str, str]] = []


def patch_error(step: str, message: str):
    """Record a patch failure.  Build will abort after all patches run."""
    _patch_errors.append((step, message))
    print(f"  ✗ PATCH FAILED: {message}")


def check_patch_errors():
    """Abort the build if any patches failed to apply."""
    if not _patch_errors:
        return
    print("\n" + "=" * 60)
    print("BUILD FAILED — the following patches did not apply:")
    print("=" * 60)
    for step, msg in _patch_errors:
        print(f"  [{step}] {msg}")
    print()
    print("This usually means marimo's output format has changed.")
    print("Check the new marimo version's exported JS for renamed")
    print("chunks, changed variable names, or restructured code.")
    print()
    print("To diagnose, run:  scripts/check_upgrade.sh [new-version]")
    sys.exit(1)


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

    if patched_count == 0:
        patch_error("cdn-urls", "No files were patched for CDN URL rewriting")
    else:
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
            patch_error("publish-button",
                        f"'Publish HTML to web' regex did not match in {path}")

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

    if patched == 0:
        patch_error("publish-button",
                    "No files contained the 'Publish HTML to web' pattern")
    else:
        print(f"  ✓ Patched {patched} files")


def _find_jotai_store(text):
    """Find the jotai store variable in a minified JS chunk.

    The jotai store is imported from a jotai/useEvent module and is the
    only import with ``.get()`` usage (used to read atom values).

    Returns the variable name, or ``None`` if not found.
    """
    # Try imports from jotai-*.js or useEvent-*.js modules
    for m in re.finditer(
        r'import\{([^}]+)\}from"\./(?:jotai|useEvent)-[^"]+\.js"', text
    ):
        for part in m.group(1).split(","):
            ident = part.strip().split(" as ")[-1].strip()
            if ident and re.search(rf'\b{re.escape(ident)}\.get\(', text):
                return ident
    return None


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

        # Identify the jotai store variable.  The store is imported from a
        # jotai-related module and is the only import with a .get() method.
        #
        # Known import formats:
        #   Old:  import{i as <store>,p as <creator>}from"./useEvent-*.js"
        #   New:  import{d as <creator>,<store>}from"./jotai-*.js"
        store = _find_jotai_store(text)
        if not store:
            patch_error("mode-url-sync",
                        f"Could not find jotai store in {path}")
            continue

        # Identify the mode atom variable.
        # It's the first `const <var>=<atom>({mode:...,"not-set"...cellAnchor` pattern.
        atom_match = re.search(
            r'const (\w+)=\w+\(\{mode:', text
        )
        if not atom_match:
            patch_error("mode-url-sync",
                        f"Could not find mode atom in {path}")
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
            patch_error("mode-url-sync",
                        f"Could not find export{{ in {path}")
            continue

        insert_pos = export_match.start()
        text = text[:insert_pos] + subscription + text[insert_pos:]
        path.write_text(text)
        patched += 1
        print(f"  ✓ Patched mode URL sync: {path}")
        print(f"    store={store}, modeAtom={mode_atom}")

    if patched == 0:
        patch_error("mode-url-sync", "No mode-*.js files were patched")
    else:
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
            patch_error("layout-url-sync",
                        f'Could not find selectedLayout:"vertical" in {path}')
            continue

        # --- 2b: Sync layout changes → URL ---

        # Store variable (same strategy as mode-*.js)
        store = _find_jotai_store(text)
        if not store:
            patch_error("layout-url-sync",
                        f"Could not find jotai store in {path}")
            continue

        # Promise variable: <var>=Promise.all
        promise_match = re.search(r'(\w+)=Promise\.all', text)
        if not promise_match:
            patch_error("layout-url-sync",
                        f"Could not find Promise.all in {path}")
            continue
        promise_var = promise_match.group(1)

        # Layout atom: valueAtom:<var> inside the layout factory
        layout_atom_match = re.search(r'valueAtom:(\w+)', text)
        if not layout_atom_match:
            patch_error("layout-url-sync",
                        f"Could not find valueAtom:<var> in {path}")
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
            patch_error("layout-url-sync",
                        f"Could not find export{{ in {path}")
            continue

        insert_pos = export_match.start()
        text = text[:insert_pos] + subscription + text[insert_pos:]
        path.write_text(text)
        patched += 1
        print(f"  ✓ Patched layout URL sync: {path}")
        print(f"    store={store}, promise={promise_var}, layoutAtom={layout_atom}")

    if patched == 0:
        patch_error("layout-url-sync", "No layout-*.js files were patched")
    else:
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
            patch_error("share-links",
                        f"Could not find share function pattern in {path}")

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
            patch_error("share-links",
                        f"Could not find </marimo-code> in {path}")

    if patched == 0:
        patch_error("share-links",
                    "No files were patched for share-link support")
    else:
        print(f"  ✓ Patched {patched} files for share-link support")


def patch_share_layout_embed(output_dir):
    """Embed grid/slides layout positions into share link code.

    When a user creates a share link from a grid or slides layout, this patch
    ensures the full cell positions (x, y, w, h) are serialized into the
    Python code as a ``layout_file="data:application/json;base64,..."``
    parameter on ``marimo.App(...)``.

    Two JS files are patched:

    1. **layout-*.js** — Exposes the internal ``getSerializedLayout()``
       function as ``window.__marimoGetSerializedLayout`` so the share
       function can call it.

    2. **share-*.js** — After the existing fallback chain (which populates
       the code variable), injects logic that:
       a) Calls ``getSerializedLayout()`` to get the current layout data
       b) JSON-stringifies and base64-encodes it into a data URI
       c) Injects ``layout_file="data:..."`` into ``marimo.App(...)`` in
          the Python source (or replaces an existing ``layout_file``).

    This function MUST run after ``patch_wasm_share_links`` because it
    depends on the error-throw anchor that function injects.
    """
    print("\n  ── Embedding layout positions in share links ──")

    # --- Part A: Expose getSerializedLayout() as a global -----------------
    for path in Path(output_dir).rglob("layout-*.js"):
        text = path.read_text(errors="ignore")

        # Find .serializeLayout( — unique to getSerializedLayout()
        ser_idx = text.find('.serializeLayout(')
        if ser_idx == -1:
            patch_error("layout-embed",
                        f"Could not find .serializeLayout( in {path}")
            continue

        # Find the enclosing function.  In minified builds this is either:
        #   wr=function(){...serializeLayout...}   (assignment expression)
        #   function wr(){...serializeLayout...}    (function declaration)
        # Try assignment form first (more common in Vite/Rollup output).
        fn_matches = list(re.finditer(
            r'(\w+)=function\(\)\{', text[:ser_idx]
        ))
        if not fn_matches:
            fn_matches = list(re.finditer(
                r'function (\w+)\(\)\{', text[:ser_idx]
            ))
        if not fn_matches:
            patch_error("layout-embed",
                        f"Could not find enclosing function "
                        f"for serializeLayout in {path}")
            continue
        fn_name = fn_matches[-1].group(1)

        # Expose as a lazy global before export{.  The function variable
        # is declared at module scope but assigned inside the TLA
        # (Promise.all().then()) callback, so it is still undefined when
        # the module body runs.  A wrapper defers the call until the user
        # actually clicks Share, by which time the TLA has resolved.
        export_match = re.search(r'export\{', text)
        if not export_match:
            patch_error("layout-embed",
                        f"Could not find export{{ in {path}")
            continue
        insert_pos = export_match.start()
        text = (text[:insert_pos]
                + f'window.__marimoGetSerializedLayout='
                + f'function(){{return {fn_name}()}};'
                + text[insert_pos:])
        path.write_text(text)
        print(f"  ✓ Exposed getSerializedLayout as global: {path}")

    # --- Part B: Inject layout embedding in share function ----------------
    for path in Path(output_dir).rglob("share-*.js"):
        text = path.read_text(errors="ignore")

        # Find the code variable from the error-throw pattern injected by
        # patch_wasm_share_links.
        var_match = re.search(
            r'if\(!(\w+)\)\{throw new Error\("Notebook still loading',
            text,
        )
        if not var_match:
            patch_error("layout-embed",
                        f"Could not find error-throw pattern in {path}")
            continue
        code_var = var_match.group(1)

        # Find the insertion point: after the error throw's closing }
        anchor = ('throw new Error("Notebook still loading. '
                  'Please wait and try again.")}')
        anchor_idx = text.find(anchor)
        if anchor_idx == -1:
            patch_error("layout-embed",
                        f"Could not find error anchor in {path}")
            continue
        insert_pos = anchor_idx + len(anchor)

        # Inject layout embedding code
        injection = (
            f'var _gsl=window.__marimoGetSerializedLayout;'
            f'if(_gsl){{var _ld=_gsl();if(_ld){{'
            f'var _lj=JSON.stringify(_ld);'
            f'var _lb=btoa(_lj);'
            f'var _luri="data:application/json;base64,"+_lb;'
            f'if({code_var}.indexOf("layout_file=")!==-1)'
            f'{code_var}={code_var}.replace(/layout_file=["\'][^"\']*["\']/,'
            f'\'layout_file="\'+_luri+\'"\');'
            f'else if({code_var}.indexOf("marimo.App(")!==-1)'
            f'{code_var}={code_var}.replace("marimo.App(",'
            f'\'marimo.App(layout_file="\'+_luri+\'\\",\');'
            f'}}}}'
        )

        text = text[:insert_pos] + injection + text[insert_pos:]
        path.write_text(text)
        print(f"  ✓ Injected layout embedding in share function: {path}")


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
                    patch_error("marimo-base",
                                f"pip download failed: {result.stderr.strip()}")
                    return
        except Exception as e:
            patch_error("marimo-base", f"Could not download marimo-base: {e}")
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
            patch_error("marimo-base",
                        f"Could not update pyodide-lock.json: {e}")


def _get_pip_env():
    """Return an environment dict with PIP_CONFIG_FILE set if pip.conf exists."""
    env = os.environ.copy()
    pip_conf = Path("pip.conf")
    if pip_conf.exists():
        env["PIP_CONFIG_FILE"] = str(pip_conf.resolve())
    return env


def _get_pypi_index_url():
    """Read a custom index-url from pip.conf, falling back to pypi.org."""
    pip_conf = Path("pip.conf")
    if pip_conf.exists():
        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        url = cfg.get("global", "index-url", fallback=None)
        if url:
            return url.rstrip("/")
    return "https://pypi.org"


def _get_pypi_proxy_handler():
    """Return a urllib ProxyHandler if pip.conf specifies a proxy."""
    pip_conf = Path("pip.conf")
    if pip_conf.exists():
        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        proxy = cfg.get("global", "proxy", fallback=None)
        if proxy:
            return urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}
            )
    return None


def _pypi_urlopen(url, timeout=30):
    """Open a URL using any proxy configured in pip.conf."""
    req = urllib.request.Request(url)
    handler = _get_pypi_proxy_handler()
    if handler:
        opener = urllib.request.build_opener(handler)
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _pyodide_marker_env():
    """Return a PEP 508 marker environment matching Pyodide's runtime."""
    return {
        "os_name": "posix",
        "sys_platform": "emscripten",
        "platform_system": "Emscripten",
        "platform_machine": "wasm32",
        "platform_release": "",
        "implementation_name": "cpython",
        "implementation_version": "3.12.1",
        "python_version": "3.12",
        "python_full_version": "3.12.1",
        "extra": "",
    }


def parse_requirements_in(filepath):
    """Parse a requirements.in-style file into a list of requirement strings.

    Strips comments (#) and blank lines.  Returns raw requirement strings
    like ``["Markdown", "narwhals>=2.0", "git+https://..."]``.
    """
    lines = []
    for raw in Path(filepath).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _pyodide_normalize(name):
    """Normalize a package name to Pyodide's lock-file key format (lowercase, hyphens)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_pyodide_lock(output_dir):
    """Load pyodide-lock.json and return (lock_data, path)."""
    p = Path(output_dir) / "pyodide" / "pyodide-lock.json"
    if p.exists():
        return json.loads(p.read_text()), p
    return None, p


def _save_pyodide_lock(lock_data, lock_path):
    """Write pyodide-lock.json back to disk."""
    lock_path.write_text(json.dumps(lock_data, indent=2))


def _pyodide_has_package(lock_data, name, specifier=None):
    """Check if pyodide-lock.json already satisfies *name* with *specifier*.

    Returns ``True`` if the bundled version matches (or no specifier given).
    """
    if lock_data is None:
        return False
    pkg_key = _pyodide_normalize(name)
    entry = lock_data.get("packages", {}).get(pkg_key)
    if entry is None:
        return False
    if specifier is None:
        return True
    from packaging.version import Version
    from packaging.specifiers import SpecifierSet
    try:
        return Version(entry["version"]) in SpecifierSet(str(specifier))
    except Exception:
        return True  # if we can't parse, assume it's fine


def _register_wheel_in_lock(lock_data, lock_path, wheel_path, name, version,
                             imports=None):
    """Add or update a wheel entry in pyodide-lock.json."""
    if lock_data is None:
        return
    pkg_key = _pyodide_normalize(name)
    pkg_import = name.lower().replace("-", "_")
    wheel_sha = hashlib.sha256(Path(wheel_path).read_bytes()).hexdigest()
    lock_data.setdefault("packages", {})[pkg_key] = {
        "name": pkg_key,
        "version": version,
        "file_name": Path(wheel_path).name,
        "install_dir": "site",
        "sha256": wheel_sha,
        "package_type": "package",
        "depends": [],
        "imports": imports or [pkg_import],
    }
    _save_pyodide_lock(lock_data, lock_path)


def _extract_wheel_metadata(wheel_path):
    """Read METADATA from a wheel and return (name, version, requires_dist, imports).

    ``requires_dist`` is a list of raw PEP 508 dependency strings (or []).
    ``imports`` is a list of top-level importable names derived from the wheel
    contents.
    """
    with zipfile.ZipFile(wheel_path) as zf:
        # Find the .dist-info/METADATA file
        metadata_path = None
        for entry in zf.namelist():
            if entry.endswith(".dist-info/METADATA"):
                metadata_path = entry
                break
        if metadata_path is None:
            return None, None, [], []

        text = zf.read(metadata_path).decode("utf-8")

        name = None
        version = None
        requires = []
        for line in text.splitlines():
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
            elif line.startswith("Requires-Dist:"):
                requires.append(line.split(":", 1)[1].strip())

        # Derive importable top-level names from wheel contents
        top_level_path = None
        for entry in zf.namelist():
            if entry.endswith(".dist-info/top_level.txt"):
                top_level_path = entry
                break
        if top_level_path:
            imports = [
                l.strip() for l in zf.read(top_level_path).decode().splitlines()
                if l.strip()
            ]
        else:
            # Guess from package directories (first-level dirs that aren't .dist-info)
            imports = sorted({
                entry.split("/")[0]
                for entry in zf.namelist()
                if "/" in entry
                and not entry.split("/")[0].endswith(".dist-info")
                and not entry.split("/")[0].endswith(".data")
            })
            if not imports:
                imports = [name.lower().replace("-", "_")] if name else []

        return name, version, requires, imports


def _filter_requires_dist(requires_dist):
    """Filter a Requires-Dist list down to dependencies needed in Pyodide.

    Drops extras-only deps, platform-specific deps that don't match
    emscripten/wasm32, and python_version markers < 3.12.
    """
    from packaging.requirements import Requirement

    marker_env = _pyodide_marker_env()
    result = []
    for dep_str in requires_dist:
        try:
            req = Requirement(dep_str)
        except Exception:
            continue
        # Skip if the marker doesn't match Pyodide's environment
        if req.marker and not req.marker.evaluate(marker_env):
            continue
        # Skip extras (we never install with extras)
        if req.extras:
            continue
        result.append(req)
    return result


def build_git_wheel(git_url, dest_dir):
    """Build a wheel from a git URL and return metadata.

    Returns ``(wheel_path, name, version, requires_dist, imports)``
    or ``None`` if the build fails or produces a non-pure wheel.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        env = _get_pip_env()
        result = subprocess.run(
            ["pip", "wheel", "--no-deps", "--wheel-dir", tmpdir, git_url],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  ⚠ pip wheel failed for {git_url}:\n{result.stderr}")
            return None

        wheels = list(Path(tmpdir).glob("*.whl"))
        if not wheels:
            print(f"  ⚠ No wheel produced for {git_url}")
            return None
        whl = wheels[0]

        # Verify it's a pure-Python wheel
        if "py3-none-any" not in whl.name and "py2.py3-none-any" not in whl.name:
            # Double-check via WHEEL metadata
            with zipfile.ZipFile(whl) as zf:
                wheel_meta = None
                for entry in zf.namelist():
                    if entry.endswith(".dist-info/WHEEL"):
                        wheel_meta = zf.read(entry).decode()
                        break
            if wheel_meta and "Tag: py3-none-any" not in wheel_meta \
                    and "Tag: py2.py3-none-any" not in wheel_meta:
                print(f"  ⚠ Skipping {whl.name}: not a pure-Python wheel")
                return None

        name, version, requires_dist, imports = _extract_wheel_metadata(whl)
        dest = Path(dest_dir) / whl.name
        shutil.copy(whl, dest)
        return dest, name, version, requires_dist, imports


def _find_best_version(pypi_data, specifier=None):
    """Find the best matching version from PyPI data given a specifier.

    Returns ``(version, wheel_info)`` or ``(None, None)``.
    """
    from packaging.version import Version
    from packaging.specifiers import SpecifierSet

    spec = SpecifierSet(str(specifier)) if specifier else SpecifierSet()

    # Try latest version first (most common case)
    latest = pypi_data["info"]["version"]
    if Version(latest) in spec:
        for u in pypi_data["releases"].get(latest, []):
            if u["filename"].endswith("-py3-none-any.whl") or \
               u["filename"].endswith("-py2.py3-none-any.whl"):
                return latest, u
        # Latest matches specifier but has no pure wheel — fall through to
        # scan other versions.

    # Scan all releases for the newest matching version with a pure wheel
    candidates = []
    for ver_str, files in pypi_data["releases"].items():
        try:
            ver = Version(ver_str)
        except Exception:
            continue
        if ver not in spec:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            continue
        for u in files:
            if u["filename"].endswith("-py3-none-any.whl") or \
               u["filename"].endswith("-py2.py3-none-any.whl"):
                candidates.append((ver, ver_str, u))
                break

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def download_pypi_package(output_dir, name, specifier=None, visited=None):
    """Download a PyPI package and its transitive dependencies.

    Returns ``True`` if the package was successfully handled, ``False`` on
    failure.  Populates *visited* to avoid processing the same package twice.
    """
    if visited is None:
        visited = {}

    from packaging.requirements import Requirement

    pkg_key = _pyodide_normalize(name)
    pyodide_dir = Path(output_dir) / "pyodide"
    lock_data, lock_path = _load_pyodide_lock(output_dir)

    # Already processed this run?
    if pkg_key in visited:
        return True

    # Already in pyodide at a satisfying version?
    if _pyodide_has_package(lock_data, name, specifier):
        bundled_ver = lock_data["packages"].get(pkg_key, {}).get("version", "?")
        print(f"  ✓ {name} {bundled_ver} already in Pyodide (satisfies {specifier or 'any'})")
        visited[pkg_key] = bundled_ver
        return True

    # Fetch from PyPI
    index_url = _get_pypi_index_url()
    # For custom indexes, fall back to standard simple API
    if "pypi.org" in index_url:
        pypi_url = f"https://pypi.org/pypi/{name}/json"
    else:
        # For custom indexes, try PyPI JSON API as fallback
        pypi_url = f"https://pypi.org/pypi/{name}/json"

    try:
        with _pypi_urlopen(pypi_url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ⚠ Failed to fetch PyPI metadata for {name}: {e}")
        return False

    version, wheel_info = _find_best_version(data, specifier)
    if version is None or wheel_info is None:
        print(f"  ⚠ No pure-Python wheel found for {name}{specifier or ''}")
        return False

    visited[pkg_key] = version

    # Check if this exact version is already downloaded
    wheel_name = wheel_info["filename"]
    wheel_dest = pyodide_dir / wheel_name
    if wheel_dest.exists():
        print(f"  ✓ {name} {version} already downloaded")
    else:
        # Remove any older wheels for this package (try both hyphen and underscore forms)
        pkg_under = name.lower().replace("-", "_")
        for old_whl in pyodide_dir.glob(f"{pkg_under}-*.whl"):
            old_whl.unlink()
        for old_whl in pyodide_dir.glob(f"{pkg_key}-*.whl"):
            old_whl.unlink()

        print(f"  ↓ Downloading {name} {version}")
        wheel_url = wheel_info["url"]
        download(wheel_url, wheel_dest)

    # Extract imports from wheel metadata
    _, _, requires_dist, imports = _extract_wheel_metadata(wheel_dest)

    # Register in pyodide-lock.json
    lock_data, lock_path = _load_pyodide_lock(output_dir)
    _register_wheel_in_lock(lock_data, lock_path, wheel_dest, name, version,
                            imports=imports)

    # Resolve transitive dependencies
    filtered_deps = _filter_requires_dist(requires_dist)
    for dep_req in filtered_deps:
        dep_key = _pyodide_normalize(dep_req.name)
        if dep_key in visited:
            continue
        # Reload lock_data each time (may have been updated by recursive calls)
        lock_data, _ = _load_pyodide_lock(output_dir)
        if _pyodide_has_package(lock_data, dep_req.name, dep_req.specifier):
            visited[dep_key] = "bundled"
            continue
        print(f"    ↳ Transitive dep: {dep_req.name}{dep_req.specifier or ''}")
        download_pypi_package(
            output_dir, dep_req.name,
            specifier=dep_req.specifier if dep_req.specifier else None,
            visited=visited,
        )

    return True


def resolve_and_download_packages(output_dir, requirements):
    """Process a list of requirement strings, downloading wheels and deps.

    Handles both PyPI packages and ``git+https://...`` URLs.
    """
    from packaging.requirements import Requirement

    visited = {}
    pyodide_dir = Path(output_dir) / "pyodide"
    lock_data, lock_path = _load_pyodide_lock(output_dir)

    for req_str in requirements:
        if req_str.startswith("git+"):
            # Git URL — build wheel, register, resolve deps
            print(f"\n  ◆ Building from git: {req_str}")
            result = build_git_wheel(req_str, pyodide_dir)
            if result is None:
                continue
            wheel_path, name, version, requires_dist, imports = result
            pkg_key = _pyodide_normalize(name)
            visited[pkg_key] = version

            # Register in lock
            lock_data, lock_path = _load_pyodide_lock(output_dir)
            _register_wheel_in_lock(lock_data, lock_path, wheel_path, name,
                                     version, imports=imports)
            print(f"  ✓ Built and registered {name} {version}")

            # Resolve transitive deps
            filtered_deps = _filter_requires_dist(requires_dist)
            for dep_req in filtered_deps:
                dep_key = _pyodide_normalize(dep_req.name)
                if dep_key in visited:
                    continue
                lock_data, _ = _load_pyodide_lock(output_dir)
                if _pyodide_has_package(lock_data, dep_req.name, dep_req.specifier):
                    visited[dep_key] = "bundled"
                    continue
                print(f"    ↳ Transitive dep: {dep_req.name}{dep_req.specifier or ''}")
                download_pypi_package(
                    output_dir, dep_req.name,
                    specifier=dep_req.specifier if dep_req.specifier else None,
                    visited=visited,
                )
        else:
            # PyPI package (possibly with version specifier)
            try:
                req = Requirement(req_str)
            except Exception as e:
                print(f"  ⚠ Could not parse requirement '{req_str}': {e}")
                continue
            spec = req.specifier if req.specifier else None
            download_pypi_package(output_dir, req.name, specifier=spec,
                                   visited=visited)


def download_wasm_extras(output_dir):
    """Download packages from requirements-wasm-extras.in and their dependencies."""
    print("\n══════════════════════════════════════════")
    print("Step 6c: Downloading extra packages")
    print("══════════════════════════════════════════")

    req_file = Path("requirements-wasm-extras.in")
    if not req_file.exists():
        print("  ℹ No requirements-wasm-extras.in found, skipping")
        return

    requirements = parse_requirements_in(req_file)
    if not requirements:
        print("  ℹ requirements-wasm-extras.in is empty, skipping")
        return

    print(f"  ℹ Found {len(requirements)} top-level requirements")
    resolve_and_download_packages(output_dir, requirements)


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
# Post-patch verification
# ---------------------------------------------------------------------------

# Domains that must NOT appear in any non-vendor file after patching.
_FORBIDDEN_DOMAINS = [
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "wasm.marimo.app",
]

# CDN URL substrings that are known-safe to leave in place.
# These are optional features that degrade gracefully when offline.
_ALLOWED_CDN_URLS = [
    # MathJax: checked conditionally (`oN("url")==="ready"`), not actively
    # loaded.  marimo uses KaTeX (bundled) for math rendering.
    "cdn.jsdelivr.net/npm/mathjax-full@",
    # Lucide icons: fetches SVGs for the icon picker in edit mode.
    # Fails silently — icons just don't render in the autocomplete.
    "cdn.jsdelivr.net/npm/lucide-static@",
]

# Markers that MUST be present in the output after patching.
_REQUIRED_MARKERS = [
    # (glob, substring, description)
    ("**/share-*.js", "Notebook still loading", "share-link error fallback"),
    ("**/share-*.js", "__marimoGetSerializedLayout", "layout embed in share"),
    ("**/layout-*.js", "__marimoGetSerializedLayout", "layout global exposure"),
    ("**/mode-*.js", "view-as", "mode URL sync"),
    ("**/layout-*.js", "searchParams", "layout URL sync"),
    ("**/index.html", 'data-marimo-share', "share-link hash handler"),
]


def verify_build(output_dir):
    """Scan the built site for leftover CDN URLs and missing patch markers.

    Any problem is recorded via ``patch_error`` so the build fails with a
    clear summary.
    """
    print("\n══════════════════════════════════════════")
    print("Verifying build output")
    print("══════════════════════════════════════════")

    output = Path(output_dir)

    # --- Check for forbidden external domains ---
    violations = []
    for path in output.rglob("*"):
        if path.suffix not in (".js", ".html", ".mjs", ".css"):
            continue
        # Skip vendor/pyodide/fonts directories (we don't patch those)
        rel = str(path.relative_to(output))
        if any(rel.startswith(d) for d in ("pyodide/", "vendor/", "fonts/")):
            continue
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        for domain in _FORBIDDEN_DOMAINS:
            pattern = f"https://{domain}"
            if pattern not in text:
                continue
            # Check each occurrence against the allowlist
            for url_m in re.finditer(
                rf'https://{re.escape(domain)}[^\s"\'`\\)]*', text
            ):
                url = url_m.group()
                if any(allowed in url for allowed in _ALLOWED_CDN_URLS):
                    continue  # known-safe, skip
                # Find the line number for diagnostics
                lineno = text[:url_m.start()].count("\n") + 1
                line = text.splitlines()[lineno - 1].strip()[:120]
                violations.append((path, domain, lineno, line))
                break  # one violation per domain per file is enough

    if violations:
        for path, domain, lineno, snippet in violations:
            patch_error("verify-cdn",
                        f"Leftover CDN URL ({domain}) in "
                        f"{path}:{lineno}: {snippet}")
    else:
        print(f"  ✓ No forbidden CDN domains found")

    # --- Check for https://marimo.app as a hardcoded default ---
    for path in output.rglob("**/share-*.js"):
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        if '"https://marimo.app"' in text:
            patch_error("verify-cdn",
                        f"Hardcoded marimo.app URL still in {path}")

    # --- Check required patch markers ---
    for glob_pat, marker, desc in _REQUIRED_MARKERS:
        found = False
        matched_files = list(output.rglob(glob_pat))
        if not matched_files:
            patch_error("verify-markers",
                        f"No files matching {glob_pat} — cannot verify {desc}")
            continue
        for path in matched_files:
            try:
                text = path.read_text(errors="ignore")
            except Exception:
                continue
            if marker in text:
                found = True
                break
        if found:
            print(f"  ✓ Found: {desc}")
        else:
            patch_error("verify-markers",
                        f"Missing marker for '{desc}' "
                        f"(expected '{marker}' in {glob_pat})")

    # --- Check publish button is hidden ---
    for path in output.rglob("*.js"):
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        if "Publish HTML to web" in text:
            m = re.search(
                r'label:"Publish HTML to web",hidden:([^,]+)', text
            )
            if m and m.group(1) != "!0":
                patch_error("verify-publish",
                            f"Publish button not hidden in {path} "
                            f"(hidden:{m.group(1)})")
            elif m:
                print(f"  ✓ Publish button hidden")

    # --- Check pyodide-lock.json has required extra packages ---
    lock_path = output / "pyodide" / "pyodide-lock.json"
    if lock_path.exists():
        lock_data = json.loads(lock_path.read_text())
        packages = lock_data.get("packages", {})
        req_file = Path("requirements-wasm-extras.in")
        if req_file.exists():
            for line in parse_requirements_in(req_file):
                if line.startswith("git+"):
                    continue  # can't easily check these by name
                from packaging.requirements import Requirement
                try:
                    req = Requirement(line)
                    pkg_key = _pyodide_normalize(req.name)
                    if pkg_key not in packages:
                        patch_error("verify-packages",
                                    f"Package '{req.name}' ({pkg_key}) "
                                    f"not in pyodide-lock.json")
                    else:
                        print(f"  ✓ Package: {pkg_key}")
                except Exception:
                    pass
    else:
        patch_error("verify-packages", "pyodide-lock.json not found")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _detect_marimo_version():
    """Return the installed marimo version, or MARIMO_VERSION as fallback."""
    for python in ("python3", "python"):
        try:
            result = subprocess.run(
                [python, "-c", "import marimo; print(marimo.__version__)"],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except FileNotFoundError:
            continue
    return MARIMO_VERSION


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

    # Verify installed marimo matches the pinned version
    marimo_version = _detect_marimo_version()
    if marimo_version != MARIMO_VERSION:
        print(f"\n⚠ WARNING: Installed marimo {marimo_version} differs from "
              f"pinned MARIMO_VERSION {MARIMO_VERSION}")
        print(f"  Patches were tested against {MARIMO_VERSION}.")
        print(f"  If the build fails, update patches or revert marimo.")
        print()

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

    # Step 6a-3: Embed layout positions in share link code
    patch_share_layout_embed(output_dir)

    # Check patch errors before proceeding to downloads (fail fast)
    check_patch_errors()

    # Step 6b: Download marimo-base for WASM support
    download_marimo_base(output_dir, marimo_version)

    # Step 6c: Download extra packages from requirements-wasm-extras.in
    download_wasm_extras(output_dir)

    # Step 7: Configure offline packages
    patch_micropip_for_offline(output_dir)

    # Step 8: Create index page
    create_index_page(output_dir, notebooks)

    # Step 9: Metadata files
    add_metadata_files(output_dir)

    # Step 10: Verify the build
    verify_build(output_dir)
    check_patch_errors()

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
    print(f"  marimo version:  {marimo_version} (pinned: {MARIMO_VERSION})")
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
