#!/usr/bin/env bash

# Build the documentation the same way the GitHub Actions workflow does.

set -euo pipefail

echo "Testing documentation build..."

if [[ ! -f "mkdocs.yml" || ! -f "docs/requirements-docs.txt" ]]; then
    echo "Error: mkdocs.yml or docs/requirements-docs.txt not found. Run this script from the project root."
    exit 1
fi

VENV_DIR=".venv-docs-test"
SITE_DIR="site"

cleanup() {
    rm -rf "$VENV_DIR"
}
trap cleanup EXIT

rm -rf "$SITE_DIR"

echo "Creating virtual environment..."
python3 -m venv "$VENV_DIR"

echo "Installing dependencies..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r docs/requirements-docs.txt

echo "Building documentation..."
CI=true "$VENV_DIR/bin/python" -m mkdocs build --strict --site-dir "$SITE_DIR"

echo ""
echo "Documentation build successful."
echo "Built site is in: $SITE_DIR"
echo ""
echo "To serve locally:"
echo "  python3 -m venv .venv-docs-test"
echo "  .venv-docs-test/bin/pip install -r docs/requirements-docs.txt"
echo "  .venv-docs-test/bin/mkdocs serve"
