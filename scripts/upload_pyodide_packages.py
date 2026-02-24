#!/usr/bin/env python3
"""
Upload Pyodide distribution to a GitLab Generic Package Registry.

Downloads the full Pyodide tarball, adds marimo-base and extra wheels,
rewrites pyodide-lock.json with absolute registry URLs, then uploads
ALL files (core runtime + packages) so the automatic build only needs
to fetch ~25MB of core files.

Usage in GitLab CI (uses CI_JOB_TOKEN automatically):
    python scripts/upload_pyodide_packages.py --pyodide-version 0.27.7

Usage locally:
    GITLAB_TOKEN=glpat-xxx GITLAB_URL=https://gitlab.example.com \\
    GITLAB_PROJECT_ID=123 \\
    python scripts/upload_pyodide_packages.py --pyodide-version 0.27.7

After uploading, configure pip.conf:
    [pyodide]
    cdn-url = https://gitlab.example.com/api/v4/projects/123/packages/generic/pyodide/0.27.7/

Then run the build (no --slim needed):
    python scripts/build.py --mode edit
"""

import argparse
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Import shared functions from build.py
sys.path.insert(0, str(Path(__file__).parent))
from build import (
    download,
    download_marimo_base,
    resolve_and_download_packages,
    _load_pyodide_lock,
    _save_pyodide_lock,
    parse_requirements_in,
    MARIMO_VERSION,
)


def _read_marimo_version():
    """Read MARIMO_VERSION from build.py."""
    return MARIMO_VERSION


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


def _list_uploadable_files(pyodide_dir):
    """List all files in the Pyodide directory suitable for upload.

    Returns a list of Path objects.  Excludes dotfiles and TypeScript
    definitions (not needed at runtime).
    """
    skip_suffixes = {".d.ts", ".d.mts"}
    files = []
    for f in sorted(pyodide_dir.iterdir()):
        if not f.is_file():
            continue
        if f.name.startswith("."):
            continue
        if any(f.name.endswith(s) for s in skip_suffixes):
            continue
        files.append(f)
    return files


def _rewrite_lock_urls(pyodide_dir, registry_base_url):
    """Rewrite every package file_name in pyodide-lock.json to an absolute registry URL.

    Core runtime files (pyodide.mjs, etc.) are NOT packages and are not affected.
    """
    lock_path = pyodide_dir / "pyodide-lock.json"
    if not lock_path.exists():
        print("  ⚠ pyodide-lock.json not found — skipping URL rewrite")
        return

    lock_data = json.loads(lock_path.read_text())
    base = registry_base_url.rstrip("/")
    rewritten = 0

    for pkg_key, entry in lock_data.get("packages", {}).items():
        file_name = entry.get("file_name", "")
        if not file_name:
            continue
        # Already absolute — skip
        if file_name.startswith("http://") or file_name.startswith("https://"):
            continue
        entry["file_name"] = f"{base}/{file_name}"
        rewritten += 1

    lock_path.write_text(json.dumps(lock_data, indent=2))
    print(f"  ✓ Rewrote {rewritten} package URLs in pyodide-lock.json")


def main():
    parser = argparse.ArgumentParser(
        description="Upload Pyodide distribution to a GitLab Generic Package Registry"
    )
    parser.add_argument(
        "--pyodide-version", required=True,
        help="Pyodide version to download and upload (e.g. 0.27.7)"
    )
    parser.add_argument(
        "--marimo-version", default=None,
        help=f"marimo version for marimo-base wheel (default: {MARIMO_VERSION})"
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
    marimo_version = args.marimo_version or _read_marimo_version()

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
    print(f"marimo version:  {marimo_version}")
    if not args.dry_run:
        print(f"GitLab API:      {api_url}")
        print(f"Project ID:      {project_id}")
        print(f"Auth:            {token_header}")
    print()

    # --- Step 1: Get the Pyodide directory ---
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
        download(tarball_url, tarball_path, retries=10)

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

    # --- Step 2: Add marimo-base wheel ---
    print("\nAdding marimo-base wheel...")
    # download_marimo_base expects output_dir (parent of pyodide/)
    output_dir = pyodide_dir.parent
    download_marimo_base(str(output_dir), marimo_version)

    # --- Step 3: Add extra wheels from requirements-wasm-extras.in ---
    req_file = Path("requirements-wasm-extras.in")
    if req_file.exists():
        requirements = parse_requirements_in(req_file)
        if requirements:
            print(f"\nAdding {len(requirements)} extra packages...")
            resolve_and_download_packages(str(output_dir), requirements)
    else:
        print("\nNo requirements-wasm-extras.in found — skipping extras")

    # --- Step 4: Rewrite lock file with registry URLs ---
    if not args.dry_run:
        registry_base_url = (
            f"{api_url}/projects/{project_id}/packages/generic/"
            f"{args.package_name}/{pyodide_version}"
        )
    else:
        registry_base_url = (
            f"https://gitlab.example.com/api/v4/projects/PROJECT/packages/generic/"
            f"{args.package_name}/{pyodide_version}"
        )
    print(f"\nRewriting pyodide-lock.json with registry URLs...")
    _rewrite_lock_urls(pyodide_dir, registry_base_url)

    # --- Step 5: List and upload all files ---
    upload_files = _list_uploadable_files(pyodide_dir)
    if not upload_files:
        print("No files found to upload.")
        sys.exit(1)

    total_size = sum(f.stat().st_size for f in upload_files)
    print(f"\nFound {len(upload_files)} files ({total_size / (1024*1024):.1f} MB)")
    print()

    # Upload
    uploaded = 0
    failed = 0
    for i, file_path in enumerate(upload_files, 1):
        size_mb = file_path.stat().st_size / (1024 * 1024)
        prefix = f"  [{i}/{len(upload_files)}]"

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
        print("Then build with: python scripts/build.py --mode edit")

    # Cleanup temp directory
    if cleanup:
        shutil.rmtree(cleanup, ignore_errors=True)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
