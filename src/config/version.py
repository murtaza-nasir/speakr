"""
Version information helper.
"""

import os



def get_version():
    # Try reading VERSION file first (works in Docker)
    try:
        with open('VERSION', 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        pass
    
    # Fall back to git tags (works in development)
    try:
        import subprocess
        return subprocess.check_output(['git', 'describe', '--tags', '--abbrev=0'], 
                                     stderr=subprocess.DEVNULL).decode().strip()
    except:
        pass
    
    # Final fallback
    return "unknown"


def release_key(version=None):
    """'v0.10.20-alpha' -> '0_10_20': the key of a release's notes in the
    locale files (whatsNew.releases.<key>). None when there is no version."""
    import re
    if version is None:
        version = get_version()
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(version or "").strip())
    return "_".join(match.groups()) if match else None



