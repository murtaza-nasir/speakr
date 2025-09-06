#!/bin/bash

# Test script to validate documentation build locally
# This mimics what the GitHub Actions workflow does

set -e

echo "Testing documentation build..."

# Check if we're in the right directory
if [ ! -f "docs/mkdocs.yml" ]; then
    echo "Error: docs/mkdocs.yml not found. Run this script from the project root."
    exit 1
fi

# Create a virtual environment for testing
echo "Creating virtual environment..."
python3 -m venv .venv-docs-test
source .venv-docs-test/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r docs/requirements-docs.txt

# Build the documentation
echo "Building documentation..."
cd docs
export CI=true  # Enable git plugin in CI mode
mkdocs build --strict --site-dir _test_site

echo ""
echo "✅ Documentation build successful!"
echo "Built site is in: docs/_test_site"
echo ""
echo "To serve locally for testing:"
echo "  cd docs && mkdocs serve"

# Cleanup
cd ..
deactivate
rm -rf .venv-docs-test

echo "Cleanup complete."