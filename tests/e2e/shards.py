"""The browser test files of the CI browser job, split into shards.

    python tests/e2e/shards.py 2 4      # the files of shard 2 of 4, one line

Every tests/e2e/test_*_ui.py file and test_browser_smoke.py runs in exactly
one shard, so a new browser test file runs without an edit here (#643). The
files are spread over the shards by their measured run time (SECONDS), the
longest first, each to the shard with the least time so far. A file that
is not in SECONDS counts DEFAULT_SECONDS until it is measured. Each shard
starts a Speakr instance of its own, so the files of one shard do not depend
on another shard. Within a shard the smoke test runs first and the other
files in name order, as in a run of all files.
"""

import sys
from pathlib import Path

E2E = Path(__file__).resolve().parent
ROOT = E2E.parent.parent

# Seconds per file (setup, calls and teardown), from a serial run of all
# files with --durations=0 (1635 s in all, November 2026). They only need to
# be right relative to each other.
SECONDS = {
    "test_app_dialog_ui.py": 9,
    "test_asr_editor_ui.py": 141,
    "test_audit_log_ui.py": 132,
    "test_browser_smoke.py": 48,
    "test_group_permission_ui.py": 99,
    "test_header_fixes_ui.py": 20,
    "test_i18n_payload_ui.py": 10,
    "test_keyboard_shortcuts_ui.py": 94,
    "test_llm_admin_ui.py": 21,
    "test_offline_recorder_ui.py": 317,
    "test_phone_layout_ui.py": 119,
    "test_service_worker_ui.py": 56,
    "test_share_page_ui.py": 33,
    "test_share_window_ui.py": 58,
    "test_speaker_dialog_ui.py": 42,
    "test_summarizing_transcript_ui.py": 7,
    "test_toast_queue_ui.py": 273,
    "test_transcript_view_ui.py": 35,
    "test_trim_ui.py": 36,
    "test_user_defaults_ui.py": 85,
}
DEFAULT_SECONDS = 60


def browser_files():
    """The files of the browser job, as paths relative to the repository."""
    files = [E2E / "test_browser_smoke.py"] + sorted(E2E.glob("test_*_ui.py"))
    return [path.relative_to(ROOT).as_posix() for path in files]


def shards(count, files=None, seconds=None):
    """`count` lists of files that together hold every file once."""
    files = browser_files() if files is None else files
    seconds = SECONDS if seconds is None else seconds
    weight = {f: seconds.get(Path(f).name, DEFAULT_SECONDS) for f in files}
    groups = [[] for _ in range(count)]
    totals = [0] * count
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        lightest = totals.index(min(totals))
        groups[lightest].append(f)
        totals[lightest] += weight[f]
    order = {f: i for i, f in enumerate(files)}
    return [sorted(group, key=order.__getitem__) for group in groups]


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: shards.py <shard, from 1> <count>")
    shard, count = int(argv[0]), int(argv[1])
    if not 1 <= shard <= count:
        sys.exit(f"shard {shard} is not between 1 and {count}")
    print(" ".join(shards(count)[shard - 1]))


if __name__ == "__main__":
    main(sys.argv[1:])
