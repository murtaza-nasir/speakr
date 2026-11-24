"""Every browser test file runs in exactly one shard of the CI browser job (#646)."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import shards  # noqa: E402

WORKFLOW = (ROOT / ".github" / "workflows" / "test.yml").read_text()


def _matrix():
    job = WORKFLOW[WORKFLOW.index("  browser-smoke-shards:"):]
    return [int(n) for n in re.search(r"shard: \[([\d, ]+)\]", job).group(1).split(",")]


def test_the_browser_files_are_every_ui_file_and_the_smoke_test():
    files = shards.browser_files()
    expected = {"tests/e2e/test_browser_smoke.py"} | {
        p.relative_to(ROOT).as_posix() for p in (ROOT / "tests" / "e2e").glob("test_*_ui.py")}
    assert set(files) == expected
    assert len(files) == len(expected) > 15


def test_each_file_is_in_exactly_one_shard():
    count = len(_matrix())
    groups = shards.shards(count)
    flat = [f for group in groups for f in group]
    assert sorted(flat) == sorted(shards.browser_files())
    assert len(flat) == len(set(flat))
    assert all(groups)


def test_the_workflow_runs_every_shard_of_the_same_count():
    count = len(_matrix())
    assert _matrix() == list(range(1, count + 1))
    assert f"python tests/e2e/shards.py ${{{{ matrix.shard }}}} {count}" in WORKFLOW
    # The aggregate keeps the required check name and fails unless every shard passed
    aggregate = WORKFLOW[WORKFLOW.index("  browser-smoke:\n"):]
    assert "needs: browser-smoke-shards" in aggregate and "if: always()" in aggregate
    assert 'test "$SHARD_RESULT" = "success"' in aggregate


def test_every_measured_file_exists():
    names = {Path(f).name for f in shards.browser_files()}
    assert set(shards.SECONDS) <= names


def test_files_are_balanced_by_time_and_a_new_file_gets_a_shard():
    files = ["a.py", "b.py", "c.py", "d.py", "new.py"]
    seconds = {"a.py": 100, "b.py": 60, "c.py": 50, "d.py": 40}
    groups = shards.shards(2, files, seconds)
    assert groups == [["a.py", "c.py"], ["b.py", "d.py", "new.py"]]


def test_the_smoke_test_runs_first_in_its_shard():
    for group in shards.shards(len(_matrix())):
        if "tests/e2e/test_browser_smoke.py" in group:
            assert group[0] == "tests/e2e/test_browser_smoke.py"
