#!/bin/sh
# static/css/tailwind.css is compiled here from static/css/tailwind.input.css
# and tailwind.config.js with the standalone Tailwind CSS CLI (no Node.js needed).
#
#   scripts/build_css.sh            # one minified build
#   scripts/build_css.sh --watch    # rebuild on template or script changes
#
# The CLI is downloaded once into .cache/tailwindcss/ (gitignored) and its
# SHA-256 is verified. The version and checksums are the same as in the
# css-stage of the Dockerfile; when the pin is changed, both places are
# updated together.
set -eu

TAILWIND_VERSION=v3.4.19
TAILWIND_SHA256_x64=4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e
TAILWIND_SHA256_arm64=e5b2d27694daa80cc52ec29553ba2c6bd43d86bd51a9d633ed24058b9c05a676

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)  ASSET=tailwindcss-linux-x64;   SHA256=$TAILWIND_SHA256_x64 ;;
    Linux-aarch64|Linux-arm64) ASSET=tailwindcss-linux-arm64; SHA256=$TAILWIND_SHA256_arm64 ;;
    *) echo "build_css.sh: unsupported platform $(uname -s)-$(uname -m); Linux x64 and arm64 are supported" >&2; exit 1 ;;
esac

CACHE="$ROOT/.cache/tailwindcss/$TAILWIND_VERSION"
CLI="$CACHE/$ASSET"
if [ ! -x "$CLI" ]; then
    mkdir -p "$CACHE"
    URL="https://github.com/tailwindlabs/tailwindcss/releases/download/$TAILWIND_VERSION/$ASSET"
    echo "Downloading $URL"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" -o "$CLI.tmp"
    else
        wget -q "$URL" -O "$CLI.tmp"
    fi
    echo "$SHA256  $CLI.tmp" | sha256sum -c - >/dev/null || {
        echo "build_css.sh: checksum mismatch for $ASSET $TAILWIND_VERSION" >&2
        rm -f "$CLI.tmp"
        exit 1
    }
    chmod +x "$CLI.tmp"
    mv "$CLI.tmp" "$CLI"
fi

"$CLI" -c tailwind.config.js -i static/css/tailwind.input.css -o static/css/tailwind.css --minify "$@"
