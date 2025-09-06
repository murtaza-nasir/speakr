#!/usr/bin/env python3
"""
Download all CDN dependencies for offline deployment
"""

import os
import requests
from pathlib import Path

# Base directory for vendor files
VENDOR_DIR = Path(__file__).parent.parent / "static" / "vendor"

# Dependencies to download
DEPENDENCIES = {
    "css": {
        "fontawesome.min.css": "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css",
        "easymde.min.css": "https://cdn.jsdelivr.net/npm/easymde/dist/easymde.min.css",
    },
    "js": {
        "tailwind.min.js": "https://cdn.tailwindcss.com/3.4.0",
        "vue.global.js": "https://unpkg.com/vue@3/dist/vue.global.js",
        "vue.global.prod.js": "https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js",
        "marked.min.js": "https://cdn.jsdelivr.net/npm/marked/marked.min.js",
        "easymde.min.js": "https://cdn.jsdelivr.net/npm/easymde/dist/easymde.min.js",
        "axios.min.js": "https://cdn.jsdelivr.net/npm/axios/dist/axios.min.js",
    }
}

# Font Awesome webfonts
FONTAWESOME_FONTS = [
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-brands-400.ttf",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-brands-400.woff2",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-regular-400.ttf",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-regular-400.woff2",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-solid-900.ttf",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-solid-900.woff2",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-v4compatibility.ttf",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-v4compatibility.woff2",
]

def download_file(url, filepath):
    """Download a file from URL to filepath"""
    print(f"Downloading {url} to {filepath}")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # Create directory if it doesn't exist
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        # Write file
        with open(filepath, 'wb') as f:
            f.write(response.content)
        print(f"  ✓ Downloaded {filepath.name}")
        return True
    except Exception as e:
        print(f"  ✗ Failed to download {url}: {e}")
        return False

def main():
    print("Downloading offline dependencies...")
    print(f"Vendor directory: {VENDOR_DIR}")
    
    # Download CSS and JS files
    for file_type, files in DEPENDENCIES.items():
        print(f"\n{file_type.upper()} Files:")
        for filename, url in files.items():
            filepath = VENDOR_DIR / file_type / filename
            download_file(url, filepath)
    
    # Download Font Awesome fonts
    print("\nFont Awesome Webfonts:")
    for url in FONTAWESOME_FONTS:
        filename = url.split("/")[-1]
        filepath = VENDOR_DIR / "fonts" / "webfonts" / filename
        download_file(url, filepath)
    
    # Update Font Awesome CSS to use local fonts
    fa_css_path = VENDOR_DIR / "css" / "fontawesome.min.css"
    if fa_css_path.exists():
        print("\nUpdating Font Awesome CSS to use local fonts...")
        with open(fa_css_path, 'r') as f:
            content = f.read()
        
        # Replace CDN URLs with local paths - handle both relative and absolute URLs
        content = content.replace(
            "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/",
            "../fonts/webfonts/"
        )
        # Also replace any relative URLs that might be in the minified CSS
        content = content.replace(
            "../webfonts/",
            "../fonts/webfonts/"
        )
        content = content.replace(
            "./webfonts/",
            "../fonts/webfonts/"
        )
        
        with open(fa_css_path, 'w') as f:
            f.write(content)
        print("  ✓ Updated Font Awesome CSS paths")
    
    print("\n✅ All dependencies downloaded successfully!")

if __name__ == "__main__":
    main()