#!/usr/bin/env python3
"""
Download the vendored JavaScript, CSS and fonts for offline deployment.

Every file is pinned to an exact version and checked against its SHA-256
checksum, and the build fails when a download fails or a checksum does not
match. A CDN serving a changed file, or a new release published under the
same name, therefore never reaches an image unnoticed.

To update a library: change the version in its URL, download the file, check
it, and replace the checksum (sha256sum <file>).
"""

import hashlib
import os
import sys
import urllib.request
from pathlib import Path

# Base directory for vendor files
VENDOR_DIR = Path(__file__).parent.parent / "static" / "vendor"

# Dependencies to download: file name -> (URL, SHA-256)
DEPENDENCIES = {
    "css": {
        "fontawesome.min.css": (
            "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css",
            "8d321d88cb97fdedc3189506c25de9292c6e73a60ebaab496243346c6404480e",
        ),
        "easymde.min.css": (
            "https://cdn.jsdelivr.net/npm/easymde@2.21.0/dist/easymde.min.css",
            "6eee36340432776d682e7372ec4a7eb29be4fdb3ffada32c0bfa5e31a5ef34a2",
        ),
        "swagger-ui.css": (
            "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css",
            "40170f0ee859d17f92131ba707329a88a070e4f66874d11365e9a77d232f6117",
        ),
    },
    "js": {
        "vue.global.js": (
            "https://cdn.jsdelivr.net/npm/vue@3.5.43/dist/vue.global.js",
            "b191cf809aa83452971804f9df2d8c67de65de34caa40f0fcf828c508c7099e2",
        ),
        "marked.min.js": (
            "https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js",
            "3e7e7d7feb3e5d58cb6c804f68ab5c24cc7e5eb6270fd6e5cbb9124739217d0c",
        ),
        "easymde.min.js": (
            "https://cdn.jsdelivr.net/npm/easymde@2.21.0/dist/easymde.min.js",
            "2c06bddfd0c89176db08ccf9d42e2beaa9b4f1a4ff8fb2ec0bf1bed25ce08e05",
        ),
        "axios.min.js": (
            "https://cdn.jsdelivr.net/npm/axios@1.20.0/dist/axios.min.js",
            "7c433c881c3b0903317193a0dd5af714ce0f65cf77d5c7f067f18b96f0e8e2c7",
        ),
        "chart.umd.min.js": (
            "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
            "d2af8974e95271638772e9e9524db5b9a6f58d6ec2d5d781400447b4a31c681e",
        ),
        "purify.min.js": (
            "https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js",
            "6407576993a5aa1303eaf9fefb95e5cfc1c0c80645bd3717db671727e6b55b91",
        ),
        "swagger-ui-bundle.js": (
            "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js",
            "c2e4a9ef08144839ff47c14202063ecfe4e59e70a4e7154a26bd50d880c88ba1",
        ),
    },
}

# Production build of Vue, used in place of the development build
VUE_PRODUCTION = (
    "https://cdn.jsdelivr.net/npm/vue@3.5.43/dist/vue.global.prod.js",
    "b72394052eef1eeda1752db3758ce1be6f88016903f22e5241cc732ea307478e",
)

# Font Awesome webfonts: file name -> (URL, SHA-256)
FONTAWESOME_FONTS = {
    "fa-brands-400.ttf": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-brands-400.ttf",
        "9e122861bc5f7d6190d580b22acc5b1c3f8cdba9f495593cccd3b47d2cbfa2fd",
    ),
    "fa-brands-400.woff2": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-brands-400.woff2",
        "29bc44694c394921d1f00271128a2e4cd8293516216e24eac07a73fa821fc1f5",
    ),
    "fa-regular-400.ttf": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-regular-400.ttf",
        "709034805d10429d24862ad685d561808328329debdbca4f70487b508320a3ff",
    ),
    "fa-regular-400.woff2": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-regular-400.woff2",
        "fa79fc4f2559112ff3dd27cf5be216d9b5f88066ceef728264f5a3a1a195fc1e",
    ),
    "fa-solid-900.ttf": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-solid-900.ttf",
        "1782156bfa2054db429277b42f143298a1ce03437c05e2779ca7875112b19d3c",
    ),
    "fa-solid-900.woff2": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-solid-900.woff2",
        "1b099f88c06ed0869872561c157f0ec9cbe133a0939d9ece4ee1e1f54bd4683d",
    ),
    "fa-v4compatibility.ttf": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-v4compatibility.ttf",
        "fde941c0964fab984f6af0697b3fec92e7df384eec9941babfd3e4cbbd6211c2",
    ),
    "fa-v4compatibility.woff2": (
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-v4compatibility.woff2",
        "b3abfccce275536beaaeba990e900a0c08ed2fefbabf4a8bcbb2e2ffe3f2431f",
    ),
}


def download_file(url, sha256, filepath):
    """Download url to filepath; raise when it fails or the checksum differs."""
    print(f"Downloading {url} to {filepath}")
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise RuntimeError(f"checksum mismatch for {url}: expected {sha256}, got {digest}")
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(data)
    print(f"  ✓ Downloaded and verified {filepath.name}")


def main():
    print("Downloading offline dependencies...")
    print(f"Vendor directory: {VENDOR_DIR}")

    # Check if we're in production mode
    is_production = os.environ.get('FLASK_ENV') == 'production' or os.environ.get('PRODUCTION') == '1'

    if is_production:
        print("⚙️  PRODUCTION MODE: Using production builds")
        # Replace Vue.js development build with production build
        DEPENDENCIES['js']['vue.global.js'] = VUE_PRODUCTION
    else:
        print("⚙️  DEVELOPMENT MODE: Using development builds")

    try:
        # Download CSS and JS files
        for file_type, files in DEPENDENCIES.items():
            print(f"\n{file_type.upper()} Files:")
            for filename, (url, sha256) in files.items():
                download_file(url, sha256, VENDOR_DIR / file_type / filename)

        # Download Font Awesome fonts
        print("\nFont Awesome Webfonts:")
        for filename, (url, sha256) in FONTAWESOME_FONTS.items():
            download_file(url, sha256, VENDOR_DIR / "fonts" / "webfonts" / filename)
    except Exception as e:
        print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)

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
