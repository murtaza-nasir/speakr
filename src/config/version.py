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



