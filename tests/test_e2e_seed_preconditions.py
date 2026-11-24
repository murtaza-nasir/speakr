"""A browser test that finds no seeded recordings fails in CI and skips elsewhere (#641).

The browser tests need what run_speakr.py gives an instance: the seeded
recordings, Inquire and public links. Against another instance they skip.
In the browser-smoke job, where run_speakr.py always started the instance,
SPEAKR_E2E_STRICT=1 turns the same miss into a failure, so a listing or
permission fault that hides the recordings cannot pass as a green run.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from browser_checks import precondition_unmet  # noqa: E402

MESSAGE = "no seeded recordings on this instance (tests/e2e/seed_recordings.py)"


def test_a_missing_precondition_fails_in_strict_mode(monkeypatch):
    monkeypatch.setenv("SPEAKR_E2E_STRICT", "1")
    with pytest.raises(pytest.fail.Exception, match="no seeded recordings"):
        precondition_unmet(MESSAGE)


@pytest.mark.parametrize("value", [None, "0", ""])
def test_a_missing_precondition_skips_outside_strict_mode(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SPEAKR_E2E_STRICT", raising=False)
    else:
        monkeypatch.setenv("SPEAKR_E2E_STRICT", value)
    with pytest.raises(pytest.skip.Exception, match="no seeded recordings"):
        precondition_unmet(MESSAGE)


def test_the_browser_job_runs_in_strict_mode():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    # The step that runs the browser files, from its name to its command
    run = workflow.index("python tests/e2e/shards.py") if "python tests/e2e/shards.py" in workflow \
        else workflow.index("tests/e2e/test_*_ui.py")
    step = workflow[workflow.rindex("- name:", 0, run):run]
    assert "SPEAKR_BROWSER_SMOKE: '1'" in step
    assert "SPEAKR_E2E_STRICT: '1'" in step


def test_no_browser_test_skips_on_missing_seed_data_directly():
    """A new check of the seeded data goes through precondition_unmet."""
    direct = re.compile(r"pytest\.skip\(\s*f?[\"'][^\"']*(seed|recordings? on this instance|needs two recordings)")
    offenders = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests" / "e2e").glob("test_*.py"))
                 if direct.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
