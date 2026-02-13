#!/usr/bin/env bash
# -----------------------------------------------------------------------
# check_upgrade.sh — Test whether a new marimo version is compatible
# with the current build patches.
#
# Usage:
#   scripts/check_upgrade.sh                  # test latest marimo
#   scripts/check_upgrade.sh 0.18.0           # test specific version
#
# What it does:
#   1. Creates a temporary virtualenv
#   2. Installs the target marimo version
#   3. Exports the notebooks
#   4. Runs ALL patches and verification
#   5. Reports pass/fail — does NOT deploy anything
#
# Exit codes:
#   0  All patches applied and verified successfully
#   1  One or more patches failed (see output for details)
# -----------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

TARGET_VERSION="${1:-}"
CURRENT_VERSION=$(grep -oP 'MARIMO_VERSION = "\K[^"]+' "$SCRIPT_DIR/build.py")

echo "========================================"
echo "marimo upgrade compatibility check"
echo "========================================"
echo "  Current pinned version: $CURRENT_VERSION"

if [ -z "$TARGET_VERSION" ]; then
    echo "  Target: latest (will resolve during install)"
else
    echo "  Target version:        $TARGET_VERSION"
fi
echo

# --- Create temp venv ---
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "Creating temporary virtualenv..."
python3 -m venv "$TMPDIR/venv"
# shellcheck disable=SC1091
source "$TMPDIR/venv/bin/activate"

# --- Install target marimo ---
if [ -z "$TARGET_VERSION" ]; then
    echo "Installing latest marimo..."
    pip install --quiet marimo
else
    echo "Installing marimo==$TARGET_VERSION..."
    pip install --quiet "marimo==$TARGET_VERSION"
fi

# Install build dependency
pip install --quiet packaging

INSTALLED=$(python -c "import marimo; print(marimo.__version__)")
echo "  Installed: marimo $INSTALLED"
echo

# --- Run the build ---
echo "Running build (--mode edit)..."
echo
cd "$PROJECT_DIR"

# The build script will:
#  - Print a warning about version mismatch (expected)
#  - Run all patches
#  - Run verification
#  - Exit non-zero if any patch failed
if python "$SCRIPT_DIR/build.py" --mode edit --output-dir "$TMPDIR/site"; then
    echo
    echo "========================================"
    echo "PASS: marimo $INSTALLED is compatible"
    echo "========================================"
    echo
    echo "To upgrade:"
    echo "  1. Update MARIMO_VERSION in scripts/build.py to \"$INSTALLED\""
    echo "  2. Update deploy.yml:  pip install marimo==$INSTALLED"
    echo "  3. Commit and push"
    exit 0
else
    echo
    echo "========================================"
    echo "FAIL: marimo $INSTALLED broke patches"
    echo "========================================"
    echo
    echo "Review the PATCH FAILED messages above."
    echo "The exported files are in: $TMPDIR/site/"
    echo "Compare with a working build to identify what changed."
    echo
    echo "Common fixes:"
    echo "  - JS chunk renamed?  Update rglob pattern"
    echo "  - Minified var changed?  Update regex"
    echo "  - Feature removed?  Remove the patch"
    # Override the trap so temp dir is preserved for debugging
    trap - EXIT
    echo "  Temp dir preserved at: $TMPDIR"
    exit 1
fi
