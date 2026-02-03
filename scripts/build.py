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

    for nb in notebooks:
        name = Path(nb).stem
        out = Path(output_dir) / f"{name}"
        print(f"\n  Exporting {nb} → {out}/")
        run(f"marimo export html-wasm {nb} -o {out} --mode {mode}")

    # If there's only one notebook, also copy its index.html to root
    if len(notebooks) == 1:
        name = Path(notebooks[0]).stem
        src = Path(output_dir) / name / "index.html"
        dst = Path(output_dir) / "index.html"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

    return notebooks


# ---------------------------------------------------------------------------
# Step 2: Detect Pyodide version
# ---------------------------------------------------------------------------

def detect_pyodide_version(output_dir):
    """Scan exported JS files to find the Pyodide CDN version being used."""
    print("\n══════════════════════════════════════════")
    print("Step 2: Detecting Pyodide version")
    print("══════════════════════════════════════════")

    pattern = re.compile(
        r"cdn\.jsdelivr\.net/pyodide/v([0-9]+\.[0-9]+\.[0-9]+)/full"
    )

    for path in Path(output_dir).rglob("*"):
        if path.suffix in (".js", ".html", ".mjs"):
            try:
                text = path.read_text(errors="ignore")
            except Exception:
                continue
            match = pattern.search(text)
            if match:
                version = match.group(1)
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
    """Download Google Fonts CSS and all referenced font files."""
    print("\n══════════════════════════════════════════")
    print("Step 4: Downloading Google Fonts")
    print("══════════════════════════════════════════")

    fonts_dir = Path(output_dir) / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)

    # Download the CSS with a user-agent that returns woff2 format
    print("  ↓ Fetching font CSS...")
    css = download_text(GOOGLE_FONTS_CSS_URL, user_agent=WOFF2_USER_AGENT)

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
    """Download KaTeX CSS and font files from jsDelivr/npm."""
    print("\n══════════════════════════════════════════")
    print("Step 5: Downloading KaTeX assets")
    print("══════════════════════════════════════════")

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
        print("  ℹ No KaTeX reference found in exports, skipping")
        return None

    print(f"  ℹ Detected KaTeX version: {katex_version}")

    katex_dir = Path(output_dir) / "vendor" / "katex"
    katex_dir.mkdir(parents=True, exist_ok=True)

    # Download main CSS
    css_url = f"https://cdn.jsdelivr.net/npm/katex@{katex_version}/dist/katex.min.css"
    css_path = katex_dir / "katex.min.css"
    download(css_url, css_path)

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

def patch_cdn_urls(output_dir, pyodide_version):
    """Rewrite all CDN URLs in exported files to relative local paths."""
    print("\n══════════════════════════════════════════")
    print("Step 6: Patching CDN URLs to local paths")
    print("══════════════════════════════════════════")

    replacements = [
        # Pyodide CDN → local pyodide/
        (
            f"https://cdn.jsdelivr.net/pyodide/v{pyodide_version}/full/",
            "../pyodide/"
        ),
        (
            f"https://cdn.jsdelivr.net/pyodide/v{pyodide_version}/full",
            "../pyodide"
        ),
        # Google Fonts CSS → local fonts/fonts.css
        (
            re.compile(
                r'https://fonts\.googleapis\.com/css2\?'
                r'family=Fira\+Mono[^"\'>\s]*'
            ),
            "../fonts/fonts.css"
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
            "../vendor/katex/"
        ),
        # PyPI for micropip — this one is trickier, handle separately
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


# ---------------------------------------------------------------------------
# Step 7: Handle PyPI/micropip for additional packages
# ---------------------------------------------------------------------------

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
    patch_cdn_urls(output_dir, pyodide_version)

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
