"""The dialogs of the open recording render only while one is open (#702).

The job-queue reconcile can clear selectedRecording while the Identify
speakers dialog or the transcript editor is open. Their templates read
selectedRecording.<field> throughout, so each dialog root requires it, and
the reconcile clears the selection only for a recording the server no longer
has (static/js/modules/utils/reconcile-selection.js).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DIALOGS = {
    'templates/modals/speaker-modal.html': 'showSpeakerModal',
    'templates/modals/asr-editor-modal.html': 'showAsrEditorModal',
}


def _root_condition(path, flag):
    text = (ROOT / path).read_text(encoding='utf-8')
    match = re.search(r'<div v-if="(%s[^"]*)"' % flag, text)
    assert match, f'{path}: no root v-if on {flag}'
    return match.group(1)


def test_each_dialog_requires_an_open_recording():
    for path, flag in DIALOGS.items():
        condition = _root_condition(path, flag)
        assert re.search(r'&&\s*selectedRecording\b', condition), (
            f'{path}: the root v-if is "{condition}"; it must also require selectedRecording')


def test_reconcile_clears_the_selection_only_through_the_helper():
    source = (ROOT / 'static/js/app.modular.js').read_text(encoding='utf-8')
    start = source.index('if (reconcileNeeded) {')
    block = source[start:source.index('}, { deep: true });', start)]
    assert 'keepSelectionAfterReconcile(' in block
    assert 'showSpeakerModal.value = false' in block
    assert 'showAsrEditorModal.value = false' in block
    assert '!recordings.value.some(' not in block
