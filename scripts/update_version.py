#!/usr/bin/env python3
"""
Update the VERSION file and the README shields.io version badge in lockstep,
then optionally create + push a git tag.

Usage:
    python scripts/update_version.py v0.9.0
    python scripts/update_version.py 0.9.0
"""
import os
import re
import subprocess
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")
README_FILE = os.path.join(REPO_ROOT, "README.md")

BADGE_RE = re.compile(
    r"(shields\.io/badge/version-)"
    r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.-]*"
    r"(-[a-z]+\.svg)"
)


def update_version(raw):
    if not re.match(r"^v?\d+\.\d+\.\d+", raw):
        print(f"Warning: '{raw}' doesn't look like X.Y.Z[-suffix]")

    tag = raw if raw.startswith("v") else f"v{raw}"
    bare = tag[1:]

    with open(VERSION_FILE, "w") as f:
        f.write(f"{tag}\n")
    print(f"VERSION → {tag}")

    if os.path.exists(README_FILE):
        with open(README_FILE) as f:
            text = f.read()
        new_text, n = BADGE_RE.subn(lambda m: f"{m.group(1)}{bare}{m.group(2)}", text, count=1)
        if n == 0:
            print("README badge not found — skipped")
        elif new_text != text:
            with open(README_FILE, "w") as f:
                f.write(new_text)
            print(f"README badge → {bare}")
        else:
            print("README badge already in sync")

    try:
        subprocess.check_output(["git", "rev-parse", "--git-dir"], stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Not in a git repo — skipping tag step")
        return True

    try:
        subprocess.check_output(["git", "tag", tag], stderr=subprocess.DEVNULL)
        print(f"Created git tag: {tag}")
        if input("Push tag to remote? (y/N): ").strip().lower() == "y":
            subprocess.check_output(["git", "push", "origin", tag])
            print(f"Pushed {tag}")
    except subprocess.CalledProcessError:
        print(f"Tag {tag} already exists locally — skipping")

    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/update_version.py vX.Y.Z[-suffix]")
        sys.exit(1)
    sys.exit(0 if update_version(sys.argv[1]) else 1)
