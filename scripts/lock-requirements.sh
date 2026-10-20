#!/bin/sh
# Regenerate requirements.lock (lite image) and requirements-embeddings.lock
# (full image) from requirements.txt, requirements-embeddings.txt and
# constraints.txt.
#
# The lock files pin every package, including the dependencies of
# dependencies, with the hash of every published file, for amd64 and arm64.
# The Dockerfile installs them with --require-hashes, so nothing is installed
# that is not in a lock file.
#
#   scripts/lock-requirements.sh                       keep existing versions; add or
#                                                      remove only what the requirements
#                                                      files changed
#   scripts/lock-requirements.sh --upgrade-package X   move X (and only what X needs)
#                                                      to its newest allowed version
#   scripts/lock-requirements.sh --check               fail when a lock file no longer
#                                                      matches the requirements (CI)
#
# Read the changelog of anything that moves before committing a new lock, and
# prefer releases that have been public for at least a week.
set -eu
cd "$(dirname "$0")/.."

UV_VERSION=0.13.3
IMAGE=python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

CHECK=0
if [ "${1:-}" = "--check" ]; then
    CHECK=1
    shift
fi

docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD":/w -w /w \
    -e UV_CUSTOM_COMPILE_COMMAND="scripts/lock-requirements.sh" \
    -e CHECK="$CHECK" "$IMAGE" sh -euc '
    pip install -q --user "uv=='"$UV_VERSION"'" >/dev/null 2>&1
    UV=/tmp/.local/bin/uv
    OUT=.
    if [ "$CHECK" = "1" ]; then
        OUT=$(mktemp -d)
        cp requirements.lock requirements-embeddings.lock "$OUT"/
    fi
    $UV pip compile -q requirements.txt -c constraints.txt \
        --universal --python-version 3.11 --generate-hashes \
        -o "$OUT"/requirements.lock "$@"
    $UV pip compile -q requirements.txt requirements-embeddings.txt -c constraints.txt \
        --universal --python-version 3.11 --generate-hashes \
        -o "$OUT"/requirements-embeddings.lock "$@"
    if [ "$CHECK" = "1" ]; then
        diff -u requirements.lock "$OUT"/requirements.lock && \
        diff -u requirements-embeddings.lock "$OUT"/requirements-embeddings.lock || {
            echo "The lock files do not match the requirements; run scripts/lock-requirements.sh" >&2
            exit 1
        }
        echo "Lock files match the requirements"
    fi
' sh "$@"
