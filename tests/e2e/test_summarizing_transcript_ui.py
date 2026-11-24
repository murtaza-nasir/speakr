"""Browser test: a recording that is SUMMARIZING shows its transcript (D#663).

The summary after a transcript runs as a job of its own, and the recording
stays SUMMARIZING, with its transcript stored, until that job ends. The page
must show the transcript in that state, and the summary tab its busy state.
The answers of the recording routes are rewritten to SUMMARIZING with no
summary, since nothing on the test instance summarizes.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL. With SPEAKR_E2E_SHOTS set, screenshots are saved there.
"""

import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")
MEETING_TITLE = TITLES[1]
SEGMENT = "[data-tv-list] [data-segment-index]"


def _summarizing(route):
    response = route.fetch()
    try:
        body = response.json()
    except Exception:
        route.fulfill(response=response)
        return
    if isinstance(body, dict):
        body["status"] = "SUMMARIZING"
        if "summary" in body:
            body["summary"] = None
    route.fulfill(response=response, body=json.dumps(body), headers={**response.headers, "content-type": "application/json"})


@pytest.mark.parametrize("viewport", [{"width": 1400, "height": 900}, {"width": 390, "height": 844}],
                         ids=["desktop", "phone"])
def test_the_transcript_is_shown_while_the_summary_runs(viewport):
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport=viewport, locale="en-US")
        page = context.new_page()
        problems = PageProblems(page, BASE)
        login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
        listing = page.request.get(f"{BASE}/api/recordings?per_page=50")
        ids = {r["title"]: r["id"] for r in listing.json().get("recordings", [])} if listing.ok else {}
        if MEETING_TITLE not in ids:
            precondition_unmet("the seeded recordings are not on this instance")
        rid = ids[MEETING_TITLE]
        page.route(re.compile(rf".*/api/recordings/{rid}(\?.*)?$"), _summarizing)
        page.route(re.compile(rf".*/recording/{rid}/status(\?.*)?$"), _summarizing)
        problems.reset()

        page.goto(f"{BASE}/recordings/{rid}")
        if viewport["width"] < 600:
            tab = re.compile(rf"^\s*{re.escape(en('transcription.title'))}\s*$")
            page.locator("button:visible").filter(has_text=tab).first.click()
        page.locator(SEGMENT).first.wait_for(state="visible", timeout=60000)
        assert page.locator(SEGMENT).count() > 0
        busy = en('summary.summaryInProgress')
        if viewport["width"] >= 600:
            summary_tab = re.compile(rf"^\s*{re.escape(en('summary.title'))}\s*$")
            page.locator("button:visible").filter(has_text=summary_tab).first.click()
            page.get_by_text(busy).first.wait_for(state="visible", timeout=10000)
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            page.screenshot(path=os.path.join(SHOTS, f"summarizing-{viewport['width']}.png"))
        check_common(page, problems, "summarizing transcript")
        context.close()
        browser.close()
