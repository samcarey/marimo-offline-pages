#!/usr/bin/env python3
"""
Upload Pyodide packages to a GitLab Generic Package Registry.

This allows --slim mode to fetch compiled WASM packages (numpy, pandas, etc.)
from your own GitLab instance instead of cdn.jsdelivr.net, making the
deployment fully air-gapped.

Usage in GitLab CI (uses CI_JOB_TOKEN automatically):
    python scripts/upload_pyodide_packages.py

Usage locally:
    GITLAB_TOKEN=glpat-xxx GITLAB_URL=https://gitlab.example.com \\
    GITLAB_PROJECT_ID=123 \\
    python scripts/upload_pyodide_packages.py

After uploading, configure pip.conf:
    [pyodide]
    cdn-url = https://gitlab.example.com/api/v4/projects/123/packages/generic/pyodide/0.27.7

Then run the build with --slim:
    python scripts/build.py --mode edit --slim
"""

import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def _read_pyodide_version_from_build():
    """Try to detect the Pyodide version from MARIMO_VERSION in build.py.

    Falls back to None if detection fails.
    """
    build_py = Path(__file__).parent / "build.py"
    if not build_py.exists():
        return None

    text = build_py.read_text()

    # Extract MARIMO_VERSION
    m = re.search(r'MARIMO_VERSION\s*=\s*"([^"]+)"', text)
    if not m:
        return None

    marimo_version = m.group(1)

    # Try to get Pyodide version from marimo's known mapping
    # For now, ask the user to provide it or read from env
    return None


def _detect_gitlab_config():
    """Read GitLab connection details from CI environment or env vars."""
    # In GitLab CI, these are provided automatically
    api_url = os.environ.get("CI_API_V4_URL")
    project_id = os.environ.get("CI_PROJECT_ID")
    token = os.environ.get("CI_JOB_TOKEN")
    token_header = "JOB-TOKEN"

    # Fall back to manual env vars
    if not api_url:
        gitlab_url = os.environ.get("GITLAB_URL", "").rstrip("/")
        if gitlab_url:
            api_url = f"{gitlab_url}/api/v4"

    if not project_id:
        project_id = os.environ.get("GITLAB_PROJECT_ID")

    if not token:
        token = os.environ.get("GITLAB_TOKEN") or os.environ.get("PRIVATE_TOKEN")
        token_header = "PRIVATE-TOKEN"

    return api_url, project_id, token, token_header


def _download_with_proxy(url, dest):
    """Download a URL, using proxy from pip.conf if available."""
    import configparser

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        print(f"  Already exists: {dest.name}")
        return

    print(f"  Downloading: {url}")
    req = urllib.request.Request(url)

    # Check pip.conf for proxy settings
    pip_conf = Path("pip.conf")
    opener = None
    if pip_conf.exists():
        cfg = configparser.ConfigParser()
        cfg.read(pip_conf)
        proxy = cfg.get("global", "proxy", fallback=None)
        if proxy:
            handler = urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy}
            )
            opener = urllib.request.build_opener(handler)

    try:
        if opener:
            resp = opener.open(req)
        else:
            resp = urllib.request.urlopen(req)
        with resp:
            data = resp.read()
        dest.write_bytes(data)
    except urllib.error.HTTPError as e:
        print(f"  Failed to download {url}: {e}")
        raise


def _upload_file(api_url, project_id, token, token_header,
                 package_name, package_version, file_path, dry_run=False):
    """Upload a single file to the GitLab Generic Package Registry.

    Returns True if uploaded, False if skipped/failed.
    """
    file_name = file_path.name
    url = (
        f"{api_url}/projects/{project_id}/packages/generic/"
        f"{package_name}/{package_version}/{file_name}"
    )

    if dry_run:
        size_mb = file_path.stat().st_size / (1024 * 1024)
        print(f"  [dry-run] Would upload: {file_name} ({size_mb:.2f} MB)")
        return True

    data = file_path.read_bytes()
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header(token_header, token)
    req.add_header("Content-Type", "application/octet-stream")

    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.getcode()
            if status in (200, 201):
                return True
            else:
                print(f"  Unexpected status {status} for {file_name}")
                return False
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # Package file already exists — skip
            return True
        print(f"  Failed to upload {file_name}: {e}")
        return False


def _list_package_files(pyodide_dir):
    """List all package files (wheels, tarballs, zips) in the Pyodide directory.

    Returns a list of Path objects, excluding core runtime files.
    """
    # Core runtime files that should NOT be uploaded (they stay on Pages)
    # These are loaded via indexURL, not through pyodide-lock.json
    core_patterns = {
        "pyodide.asm.wasm", "pyodide.asm.js",
        "pyodide.mjs", "pyodide.js",
        "pyodide-lock.json",
        "python_stdlib.zip",
        "pyodide.d.ts", "pyodide.d.mts",
        "ffi.d.ts",
        "repodata.json",
    }

    package_files = []
    for f in sorted(pyodide_dir.iterdir()):
        if not f.is_file():
            continue
        if f.name in core_patterns:
            continue
        if f.name.startswith("."):
            continue
        # Package files are .whl, .tar, .zip, or other data files
        # Include everything that's not a core runtime file
        if f.suffix in (".whl", ".tar", ".zip", ".gz", ".bz2"):
            package_files.append(f)
        elif f.name.endswith(".tar.gz") or f.name.endswith(".tar.bz2"):
            package_files.append(f)

    return package_files


def main():
    parser = argparse.ArgumentParser(
        description="Upload Pyodide packages to a GitLab Generic Package Registry"
    )
    parser.add_argument(
        "--pyodide-version", required=True,
        help="Pyodide version to download and upload (e.g. 0.27.7)"
    )
    parser.add_argument(
        "--package-name", default="pyodide",
        help="Registry package name (default: pyodide)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List files that would be uploaded without uploading"
    )
    parser.add_argument(
        "--from-dir",
        help="Use an already-extracted Pyodide directory instead of downloading"
    )
    args = parser.parse_args()

    pyodide_version = args.pyodide_version

    # Resolve GitLab connection
    api_url, project_id, token, token_header = _detect_gitlab_config()

    if not args.dry_run:
        missing = []
        if not api_url:
            missing.append("CI_API_V4_URL or GITLAB_URL")
        if not project_id:
            missing.append("CI_PROJECT_ID or GITLAB_PROJECT_ID")
        if not token:
            missing.append("CI_JOB_TOKEN, GITLAB_TOKEN, or PRIVATE_TOKEN")
        if missing:
            print(f"Error: Missing required environment variables: {', '.join(missing)}")
            print()
            print("In GitLab CI, these are set automatically.")
            print("Locally, set GITLAB_URL, GITLAB_PROJECT_ID, and GITLAB_TOKEN.")
            sys.exit(1)

    print(f"Pyodide version: {pyodide_version}")
    if not args.dry_run:
        print(f"GitLab API:      {api_url}")
        print(f"Project ID:      {project_id}")
        print(f"Auth:            {token_header}")
    print()

    # Get the Pyodide directory
    if args.from_dir:
        pyodide_dir = Path(args.from_dir)
        if not pyodide_dir.is_dir():
            print(f"Error: Directory not found: {pyodide_dir}")
            sys.exit(1)
        cleanup = None
    else:
        # Download and extract to a temp directory
        tmp_dir = tempfile.mkdtemp(prefix="pyodide-upload-")
        cleanup = tmp_dir

        tarball_url = (
            f"https://github.com/pyodide/pyodide/releases/download/"
            f"{pyodide_version}/pyodide-{pyodide_version}.tar.bz2"
        )
        tarball_path = Path(tmp_dir) / f"pyodide-{pyodide_version}.tar.bz2"

        print("Downloading Pyodide distribution...")
        _download_with_proxy(tarball_url, tarball_path)

        print("Extracting...")
        with tarfile.open(tarball_path, "r:bz2") as tar:
            tar.extractall(path=tmp_dir)

        # Find the extracted directory
        pyodide_dir = Path(tmp_dir) / "pyodide"
        if not pyodide_dir.exists():
            alt = Path(tmp_dir) / f"pyodide-{pyodide_version}"
            if alt.exists():
                pyodide_dir = alt
            else:
                for d in Path(tmp_dir).iterdir():
                    if d.is_dir() and d.name.startswith("pyodide"):
                        pyodide_dir = d
                        break

        # Clean up tarball to save disk space
        tarball_path.unlink(missing_ok=True)

    if not pyodide_dir.exists():
        print(f"Error: Pyodide directory not found at {pyodide_dir}")
        sys.exit(1)

    # List package files
    package_files = _list_package_files(pyodide_dir)
    if not package_files:
        print("No package files found to upload.")
        sys.exit(1)

    total_size = sum(f.stat().st_size for f in package_files)
    print(f"Found {len(package_files)} package files ({total_size / (1024*1024):.1f} MB)")
    print()

    # Upload
    uploaded = 0
    failed = 0
    for i, file_path in enumerate(package_files, 1):
        size_mb = file_path.stat().st_size / (1024 * 1024)
        prefix = f"  [{i}/{len(package_files)}]"

        if args.dry_run:
            print(f"{prefix} {file_path.name} ({size_mb:.2f} MB)")
            uploaded += 1
            continue

        print(f"{prefix} Uploading {file_path.name} ({size_mb:.2f} MB)...", end="", flush=True)
        ok = _upload_file(
            api_url, project_id, token, token_header,
            args.package_name, pyodide_version, file_path,
            dry_run=False
        )
        if ok:
            print(" ok")
            uploaded += 1
        else:
            print(" FAILED")
            failed += 1

    print()
    if args.dry_run:
        print(f"Dry run complete: {uploaded} files would be uploaded")
    else:
        print(f"Upload complete: {uploaded} succeeded, {failed} failed")

    if not args.dry_run and failed == 0:
        registry_url = (
            f"{api_url}/projects/{project_id}/packages/generic/"
            f"{args.package_name}/{pyodide_version}"
        )
        print()
        print("Configure pip.conf to use this registry:")
        print()
        print("  [pyodide]")
        print(f"  cdn-url = {registry_url}/")
        print()
        print("Then build with: python scripts/build.py --mode edit --slim")

    # Cleanup temp directory
    if cleanup:
        import shutil
        shutil.rmtree(cleanup, ignore_errors=True)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
