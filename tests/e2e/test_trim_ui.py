"""Browser test of trim mode in the segment editor's timeline (#660).

The server tests (tests/test_recording_trim.py) cut real audio; this test
drives the editor a user sees: the Trim button of the timeline, the start
handle dragged on the waveform, an end time typed in its field, the counts of
removed and shortened segments, Preview (the audio plays a few seconds from
the start), Esc and Cancel, the question before the cut (the app's own
dialog, saying that the removed audio cannot be recovered), and the trim
itself: the editor closes, the recording is loaded again with its shorter
audio and its transcript moved to start at 0:00. Unsaved edits are refused
first. Screenshots of trim mode are taken in all six themes and at phone
width.

It cuts the seeded recording "Browser test: trim" (seed_recordings.py), not
the ones the other tests use; on an instance without it the test is skipped.
A second run against the same instance trims the already trimmed recording
again, relative to its length, until it is shorter than 20 seconds.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test.
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

from browser_checks import PageProblems, check_common, clear_finished_jobs, en, precondition_unmet  # noqa: E402
from seed_recordings import TRIM_TITLE  # noqa: E402
from test_asr_editor_ui import BASE, CANVAS, DIALOG, TL, Editor  # noqa: E402

THEMES = ("default", "cards", "classic", "desk", "focus", "workspace")
BAR = "[data-asr-trim]"


def trim_id(editor):
    listing = editor.page.request.get(f"{BASE}/api/recordings?per_page=50")
    items = listing.json().get("recordings", []) if listing.ok else []
    found = [r["id"] for r in items if r.get("title") == TRIM_TITLE]
    return found[0] if found else None


def recording(editor, recording_id):
    response = editor.page.request.get(f"{BASE}/api/recordings/{recording_id}")
    assert response.ok
    return response.json()


def set_theme(editor, theme):
    response = editor.page.request.post(f"{BASE}/api/user/preferences",
                                        headers={"X-CSRFToken": editor.csrf(), "Content-Type": "application/json"},
                                        data=json.dumps({"ui_theme": theme}))
    assert response.ok, f"could not save the theme {theme}: HTTP {response.status}"


def enter_trim(editor):
    editor.tl_ready(waveform=False)
    editor.action("trim").click()
    editor.page.locator(BAR).wait_for(state="visible", timeout=5000)
    editor.page.wait_for_function(f"() => {TL}.trim !== null", timeout=5000)


def field(editor, edge):
    return editor.page.locator(f"[data-asr-trim-{edge}]")


def type_time(editor, edge, text):
    box = field(editor, edge)
    box.fill(text)
    box.press("Enter")
    editor.page.wait_for_timeout(100)


def clear_trim_jobs(session):
    """Remove the admin's finished jobs, so the processing queue of the
    other browser test files is as an instance without this file leaves it
    (a finished trim shows in the queue widget and changes the layout).
    No other job of the admin runs on this instance. It uses a session that
    is signed in already: the login form is rate limited."""
    page = session.page
    clear_finished_jobs(page.request, BASE)
    status = page.request.get(f"{BASE}/api/recordings/job-queue-status")
    assert status.ok
    left = [j for j in status.json().get("jobs", []) if j.get("job_type") == "trim"]
    assert not left, f"trim jobs left in the queue: {left}"


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


def signed_in_like(other, viewport):
    """An editor session in a new context with the cookies of a signed-in
    one: no second pass through the login form, which allows ten requests
    a minute (a file that signs in three times in half a minute makes the
    next file's first login fail)."""
    session = Editor.__new__(Editor)
    session.context = other.context.browser.new_context(viewport=viewport, locale="en-US",
                                                        storage_state=other.context.storage_state())
    session.page = session.context.new_page()
    session.problems = PageProblems(session.page, BASE)
    session.native = []
    session.page.on("dialog", session._dialog)
    session.ids = other.ids
    return session


@pytest.fixture(scope="module")
def desk(browser):
    """The signed-in admin on a desktop window, for every test of this file
    (one login); afterwards the finished trim jobs are removed."""
    session = Editor(browser, {"width": 1400, "height": 900})
    session.trim_id = trim_id(session)
    if session.trim_id is None:
        session.close()
        precondition_unmet("no seeded trim recording on this instance (tests/e2e/seed_recordings.py)")
    try:
        yield session
    finally:
        try:
            clear_trim_jobs(session)
        finally:
            session.close()


def test_trim_mode_drag_type_preview_and_cut(desk):
    editor = desk
    page = editor.page
    rid = editor.trim_id
    before = recording(editor, rid)
    total = float(before["audio_duration"])
    segments_before = json.loads(before["transcription"])
    if total < 20:
        precondition_unmet(f"the trim recording was trimmed to {total:.0f} s by earlier runs; start a new instance")

    editor.open(rid)
    editor.tl_ready()
    # The button is in the timeline's bar; trim mode starts with the whole recording
    trim_button = editor.action("trim")
    assert trim_button.inner_text().strip() == en("asrEditor.trim.button")
    enter_trim(editor)
    assert editor.tl("trim") == {"start": 0, "end": pytest.approx(total, abs=0.01)}
    assert field(editor, "start").input_value() == "0:00.000"
    assert page.locator("[data-asr-trim-effect]").inner_text().strip() == \
        en("asrEditor.trim.effect").replace("{{removed}}", "0").replace("{{clipped}}", "0")
    assert trim_button.get_attribute("aria-pressed") == "true"
    editor.shot("trim-test-mode")

    # The start handle, dragged on the waveform band
    box = page.locator(CANVAS).bounding_box()
    wave_y = box["y"] + editor.page.evaluate(f"() => {TL}.waveY()")
    start_x = box["x"] + editor.page.evaluate(f"() => {TL}.xOf(0)")
    target_x = box["x"] + editor.page.evaluate(f"() => {TL}.xOf(4)")
    editor.tl_drag((start_x + 1, wave_y), (target_x, wave_y))
    dragged = editor.tl("trim")["start"]
    assert dragged == pytest.approx(4, abs=0.6), f"the start handle is at {dragged}"
    assert field(editor, "start").input_value() != "0:00.000"
    # Nothing in the transcript was edited by the drag
    assert editor.tl("undoDepth") == 0

    # Typed times: the start of the second segment and the end of the tenth
    end = min(60.0, total - 5)
    type_time(editor, "start", "6")
    type_time(editor, "end", f"{int(end // 60)}:{end % 60:06.3f}")
    assert editor.tl("trim") == {"start": 6, "end": pytest.approx(end, abs=0.001)}
    assert field(editor, "start").input_value() == "0:06.000"
    expected_removed = sum(1 for s in segments_before if s["end_time"] <= 6 or s["start_time"] >= end)
    expected_clipped = sum(1 for s in segments_before
                           if not (s["end_time"] <= 6 or s["start_time"] >= end) and (s["start_time"] < 6 or s["end_time"] > end))
    assert page.locator("[data-asr-trim-effect]").inner_text().strip() == en("asrEditor.trim.effect") \
        .replace("{{removed}}", str(expected_removed)).replace("{{clipped}}", str(expected_clipped))
    # A time that is not a time marks its field and changes nothing
    type_time(editor, "end", "soon")
    assert field(editor, "end").get_attribute("aria-invalid") == "true"
    assert editor.tl("trim")["end"] == pytest.approx(end, abs=0.001)
    type_time(editor, "end", f"{int(end // 60)}:{end % 60:06.3f}")
    assert field(editor, "end").get_attribute("aria-invalid") == "false"

    # Preview: a few seconds from the start of the part that is kept
    editor.action("trim-preview").click()
    page.wait_for_function("() => { const a = document.querySelector('[data-asr-editor-audio]'); "
                           "return a && !a.paused && a.currentTime >= 6 && a.currentTime < 9.5; }", timeout=10000)
    page.evaluate("() => document.querySelector('[data-asr-editor-audio]').pause()")
    editor.shot("trim-test-range")

    # Esc leaves trim mode; the dialog stays open
    page.locator(CANVAS).focus()
    page.keyboard.press("Escape")
    page.locator(BAR).wait_for(state="detached", timeout=5000)
    assert editor.tl("trim") is None and page.locator(".asr-editor-modal").count() == 1

    # Unsaved edits: the trim waits for them to be saved
    enter_trim(editor)
    type_time(editor, "start", "6")
    editor.text_box(3).click()
    page.keyboard.type(" edited")
    assert editor.dirty()
    editor.action("trim-apply").click()
    page.wait_for_function("(text) => [...document.querySelectorAll('.toast, [role=status], [role=alert]')]"
                           ".some((el) => el.textContent.includes(text))", arg=en("asrEditor.trim.saveFirst"), timeout=5000)
    assert page.locator(DIALOG).count() == 0
    editor.action("undo").click()
    page.wait_for_function("() => !document.querySelector('[data-asr-dirty]')", timeout=5000)
    editor.action("trim-cancel").click()
    page.locator(BAR).wait_for(state="detached", timeout=5000)

    # The cut: the question first, which can be declined
    enter_trim(editor)
    type_time(editor, "start", "6")
    type_time(editor, "end", f"{int(end // 60)}:{end % 60:06.3f}")
    editor.action("trim-apply").click()
    question = page.locator(DIALOG)
    question.wait_for(state="visible", timeout=5000)
    text = question.inner_text()
    assert "cannot be recovered" in text and "0:06.000" in text, text
    # The dialog fades in: the screenshot waits for its own animations (the spinner of the Trim button runs on)
    page.wait_for_function("() => { const d = document.querySelector('[data-app-dialog]'); "
                           "return !!d && d.getAnimations({ subtree: true }).every((a) => a.playState !== 'running'); }", timeout=5000)
    page.wait_for_timeout(300)
    editor.shot("trim-test-question")
    question.locator("[data-app-dialog-cancel]").click()
    question.wait_for(state="detached", timeout=5000)
    assert page.locator(BAR).count() == 1 and recording(editor, rid)["audio_duration"] == before["audio_duration"]

    editor.action("trim-apply").click()
    question.wait_for(state="visible", timeout=5000)
    with page.expect_response(lambda r: r.url.endswith(f"/api/recordings/{rid}/trim") and r.request.method == "POST") as sent:
        question.locator("[data-app-dialog-confirm]").click()
    assert sent.value.status == 202
    page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)
    page.wait_for_function("(text) => [...document.querySelectorAll('.toast, [role=status], [role=alert]')]"
                           ".some((el) => el.textContent.includes(text))", arg=en("asrEditor.trim.done"), timeout=60000)

    after = recording(editor, rid)
    assert float(after["audio_duration"]) == pytest.approx(end - 6, abs=0.1)
    stored = json.loads(after["transcription"])
    kept = [s for s in segments_before if not (s["end_time"] <= 6 or s["start_time"] >= end)]
    assert [s["sentence"] for s in stored] == [s["sentence"] for s in kept]
    assert stored[0]["start_time"] == pytest.approx(max(0.0, kept[0]["start_time"] - 6), abs=0.001)
    # The page shows the trimmed recording: its player has the new length
    page.wait_for_function("(d) => [...document.querySelectorAll('audio')].some((a) => Math.abs(a.duration - d) < 0.3)",
                           arg=end - 6, timeout=20000)
    # and the length it shows is the new one
    kept_length = int(end - 6)
    shown = f"{kept_length // 60}:{kept_length % 60:02d}"
    editor.shot("trim-test-done")
    page.wait_for_function("(text) => document.body.innerText.includes(text)", arg=shown, timeout=10000)
    editor.check("trim")


def test_trim_mode_in_every_theme_and_on_a_phone(desk):
    session = desk
    phone = None
    try:
        rid = session.trim_id
        session.page.goto(f"{BASE}/recordings/{rid}")
        saved = session.page.locator("html").get_attribute("data-ui-theme") or "default"
        try:
            for theme in THEMES:
                set_theme(session, theme)
                session.open(rid)
                assert session.page.locator("html").get_attribute("data-ui-theme") == theme
                enter_trim(session)
                type_time(session, "start", "2")
                session.page.wait_for_timeout(300)
                session.shot(f"trim-test-theme-{theme}")
                bar = session.page.locator(BAR).bounding_box()
                modal = session.page.locator(".asr-editor-modal").bounding_box()
                assert bar["x"] >= modal["x"] - 0.5 and bar["x"] + bar["width"] <= modal["x"] + modal["width"] + 0.5
                session.check(f"trim mode, {theme} theme")
                session.action("trim-cancel").click()
                session.page.locator(BAR).wait_for(state="detached", timeout=5000)
                session.page.locator(".asr-editor-modal .modal-close").click()
                session.page.locator(".asr-editor-modal").wait_for(state="detached", timeout=10000)
        finally:
            set_theme(session, saved)

        phone = signed_in_like(session, {"width": 390, "height": 800})
        phone.page.goto(f"{BASE}/recordings/{rid}")
        set_theme(phone, "default")
        phone.open(rid)
        assert phone.page.locator("html").get_attribute("data-ui-theme") == "default"
        enter_trim(phone)
        type_time(phone, "start", "1")
        page = phone.page
        # Trim is enabled and filled with the danger colour, as on the desktop
        apply = phone.action("trim-apply")
        assert apply.is_enabled()
        colours = apply.evaluate("(el) => { const c = getComputedStyle(el); return [c.backgroundColor, c.color, c.opacity]; }")
        assert colours[1] == "rgb(255, 255, 255)" and colours[2] == "1", f"the Trim button looks disabled: {colours}"
        widths = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        assert widths[0] <= widths[1], f"the editor is wider than the phone: {widths}"
        for name in ("trim-preview", "trim-cancel", "trim-apply"):
            box = phone.action(name).bounding_box()
            assert box["height"] >= 39.5 and box["x"] + box["width"] <= 390.5, f"{name}: {box}"
        start = field(phone, "start").bounding_box()
        assert start["height"] >= 39.5 and start["x"] + start["width"] <= 390.5, f"the start field: {start}"
        # A handle is dragged with touch
        canvas = page.locator(CANVAS).bounding_box()
        y = canvas["y"] + page.evaluate(f"() => {TL}.waveY()")
        # The end handle, dragged back by a third of the part that is kept
        before = phone.tl("trim")
        target = before["end"] - (before["end"] - before["start"]) / 3
        x = canvas["x"] + page.evaluate(f"() => {TL}.xOf({TL}.trim.end)")
        to = canvas["x"] + page.evaluate(f"(t) => {TL}.xOf(t)", target)
        page.evaluate("""([x, y, to]) => {
            const c = document.querySelector('[data-asr-timeline-canvas]');
            const ev = (type, px) => new PointerEvent(type, { bubbles: true, pointerId: 7, pointerType: 'touch', clientX: px, clientY: y, isPrimary: true });
            c.dispatchEvent(ev('pointerdown', x - 4));
            for (let i = 1; i <= 6; i++) c.dispatchEvent(ev('pointermove', x - 4 + (to - x) * i / 6));
            c.dispatchEvent(ev('pointerup', to));
        }""", [x, y, to])
        assert phone.tl("trim")["end"] == pytest.approx(target, abs=0.5)
        assert phone.tl("trim")["start"] == before["start"]
        phone.shot("trim-test-phone")
        check_common(page, phone.problems, "trim mode on a phone")
        phone.action("trim-cancel").click()
    finally:
        if phone:
            phone.close()
