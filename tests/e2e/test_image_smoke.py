"""The built Docker image starts and serves the app.

Run against a container of the image, started with the example settings and
an empty database (the image-smoke job of .github/workflows/test.yml and the
smoke-lite job of docker-publish.yml). It answers one question: does the
image start and serve its pages with every script and style it refers to?
The tests are the page checks of test_browser_smoke.py that need no data.
Tests of features do not belong here: they run against a seeded instance
started from source.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_browser_smoke import (  # noqa: E402,F401
    browser_page,
    test_inquire_page,
    test_main_view,
    test_settings_pages,
)

# The skip mark of test_browser_smoke.py belongs to that module and does not
# come along with the imported tests.
pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="image smoke test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")
