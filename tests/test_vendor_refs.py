"""Every vendor file the code references is one the image build downloads.

static/vendor/ is not in git: scripts/download_offline_deps.py fetches it
when the image is built. A reference to a vendor file that script does not
download works on a developer machine that happens to have the file, and
returns 404 in the released image. (v0.10.8 loaded vendor/js/vue.global.prod.js
for the shared page header, so the account and admin headers stayed empty.)
"""

import importlib.util
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = re.compile(r"vendor/(js|css|fonts|webfonts)/([A-Za-z0-9_.\-]+)")


def _downloaded():
    spec = importlib.util.spec_from_file_location("deps", os.path.join(ROOT, "scripts", "download_offline_deps.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = set()
    for group in mod.DEPENDENCIES.values():
        if isinstance(group, dict):
            names.update(group.keys())
    return names


def _references():
    refs = {}
    for top in ("templates", os.path.join("static", "js"), os.path.join("static", "css")):
        for dirpath, _, files in os.walk(os.path.join(ROOT, top)):
            if "vendor" in dirpath.split(os.sep):
                continue
            for name in files:
                if not name.endswith((".html", ".js", ".css")):
                    continue
                path = os.path.join(dirpath, name)
                text = open(path, encoding="utf-8", errors="ignore").read()
                for kind, fname in REF.findall(text):
                    refs.setdefault(f"{kind}/{fname}", set()).add(os.path.relpath(path, ROOT))
    return refs


def test_referenced_vendor_files_are_downloaded_by_the_build():
    downloaded = _downloaded()
    missing = {ref: sorted(where) for ref, where in _references().items()
               if ref.split("/", 1)[0] == "js" and ref.split("/", 1)[1] not in downloaded}
    assert not missing, f"vendor files referenced but not downloaded at build: {missing}"


def test_the_page_header_loads_the_downloaded_vue_build():
    text = open(os.path.join(ROOT, "static", "js", "global-header.js"), encoding="utf-8").read()
    assert "vendor/js/vue.global.js" in text
    assert "vue.global.prod.js" not in text
