"""The messages sent to the LLM, pinned against a capture from master.

tests/fixtures/llm_messages/prefix_cache_layout.json holds the messages that
master e57ff27b (before build_transcript_prompt_parts and prefix_cache_flags
were introduced) sent for the title, the summary, the translation of a
segmented and of a plain transcript, and both speaker-identification paths,
in each combination of PREFIX_CACHE_OPTIMIZED_PROMPTS and
TEXT_MODEL_CACHE_BREAKPOINTS. The same inputs must still produce the same
messages, byte for byte.

The instructions that an administrator can edit (title, summary, contextual
speaker guidance) are fixed strings here, so the fixture pins the layout and
the built-in wording around them, not the shipped default prompts.

After an intended change of a prompt, the fixture is written again with
    CAPTURE_LLM_MESSAGES=1 python -m pytest tests/test_llm_messages_unchanged.py
and the difference is reviewed before it is committed.
"""

import json
import os
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.tasks.processing as processing
from src.services import transcription_translation as tt
from src.services.speaker_identification import identify_speakers_from_transcript

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'fixtures', 'llm_messages', 'prefix_cache_layout.json',
)

# (PREFIX_CACHE_OPTIMIZED_PROMPTS, TEXT_MODEL_CACHE_BREAKPOINTS)
MODES = {
    'off': (False, False),
    'breakpoints_without_prefix_cache': (False, True),
    'prefix_cache': (True, False),
    'prefix_cache_with_breakpoints': (True, True),
}

SEGMENTS = [
    {"speaker": "S01", "sentence": "Maha, can you review the \"Q3\" budget? Ça va?"},
    {"speaker": "S02", "sentence": "Yes, I will send it today."},
    {"speaker": "S01", "sentence": ""},
    {"speaker": "S02", "sentence": "The 14th works for the launch."},
]
PLAIN_TEXT = (
    "Alice: Let's lock the launch date.\n\n"
    "Bob: I propose the 14th.\n\n"
    "Alice: Agreed, the 14th it is. This is long enough to summarize."
)
FIXED_NOW = datetime(2026, 11, 15, 9, 30, 0)


def _completion(content):
    stub = MagicMock()
    stub.choices = [MagicMock()]
    stub.choices[0].message.content = content
    stub.choices[0].message.reasoning = None
    stub.choices[0].finish_reason = 'stop'
    stub.usage = MagicMock()
    stub.usage.prompt_tokens = 1
    stub.usage.completion_tokens = 1
    stub.usage.total_tokens = 2
    return stub


def _text(content):
    if isinstance(content, list):
        return ''.join(part['text'] for part in content)
    return content


def _recorder(reply):
    calls = []

    def fake(messages=None, **kwargs):
        # A deep copy through JSON: what is compared is what would be sent.
        calls.append(json.loads(json.dumps(messages)))
        return _completion(reply(messages) if callable(reply) else reply)

    return calls, fake


def _echo_translation(messages):
    """Answer a translation call with the same keys, as a model would."""
    text = _text(messages[-1]['content'])
    payload = json.loads(text[text.find('{'):text.rfind('}') + 1])
    return json.dumps({key: f'{value} [fr]' for key, value in payload.items()})


def _settings(key, default=None):
    if key == 'transcript_length_limit':
        return 30000
    if key == 'admin_default_contextual_speaker_prompt':
        return 'GUIDANCE: use only clear contextual clues.'
    return default


@pytest.fixture
def recording():
    from src.app import app, db
    from src.models import Recording, User

    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'msgpin_{suffix}', email=f'msgpin_{suffix}@local.test',
                    name='Dana Reyes', job_title='Analyst', company='Example Co',
                    output_language='French')
        user.password = 'unused'
        db.session.add(user)
        db.session.commit()
        row = Recording(
            user_id=user.id,
            title='Launch planning',
            original_filename='launch.wav',
            transcription=json.dumps(SEGMENTS, ensure_ascii=False),
            status='COMPLETED',
        )
        db.session.add(row)
        db.session.commit()
        try:
            yield app, row, user
        finally:
            db.session.rollback()
            db.session.delete(db.session.get(Recording, row.id))
            db.session.delete(db.session.get(User, user.id))
            db.session.commit()


def _title_and_summary(app, row, user):
    calls, fake = _recorder('Launch date locked')
    with patch('src.tasks.processing.call_llm_completion', side_effect=fake), \
         patch('src.tasks.processing.resolve_title_instructions',
               return_value=('- Keep it under eight words', 'test')), \
         patch('src.utils.timezones.now_local', return_value=FIXED_NOW):
        processing._generate_ai_title(row)
        title = calls[-1]
        calls.clear()
        processing.generate_summary_only_task(
            app.app_context(), row.id,
            custom_prompt_override='Summarize the decisions in three bullets.',
            user_id=user.id)
        summary = calls[0]
    return title, summary


def _translations():
    calls, fake = _recorder(_echo_translation)
    with patch('src.services.llm.call_translation_completion', side_effect=fake):
        tt.translate_json_segments(
            json.dumps(SEGMENTS, ensure_ascii=False), user_id=None, target_language='French')
        segments = list(calls)
        calls.clear()
        tt.translate_plain_text(PLAIN_TEXT, user_id=None, target_language='French')
        plain = list(calls)
    return segments, plain


def _speaker_identification(app, candidate_names):
    calls, fake = _recorder('{"SPEAKER_00": "", "SPEAKER_01": "Maha"}')
    with app.app_context(), \
         patch.dict(os.environ, {'AUTO_IDENTIFY_RESPONSE_SCHEMA': ''}), \
         patch('src.services.llm.call_llm_completion', side_effect=fake), \
         patch('src.models.SystemSetting.get_setting', side_effect=_settings):
        identify_speakers_from_transcript(SEGMENTS, user_id=1, candidate_names=candidate_names)
    return calls[0]


def _capture(app, row, user):
    captured = {}
    for mode, (prefix_cache, breakpoints) in MODES.items():
        with patch.object(processing, 'PREFIX_CACHE_OPTIMIZED_PROMPTS', prefix_cache), \
             patch.object(processing, 'TEXT_MODEL_CACHE_BREAKPOINTS', breakpoints):
            title, summary = _title_and_summary(app, row, user)
            segments, plain = _translations()
            captured[mode] = {
                'title': title,
                'summary': summary,
                'translation_segments': segments,
                'translation_plain_text': plain,
                'speaker_identification_contextual': _speaker_identification(
                    app, ['Maha', 'Jane Smith']),
                'speaker_identification_manual': _speaker_identification(app, None),
            }
    return captured


def test_llm_messages_are_identical_to_the_capture_from_master(recording):
    app, row, user = recording
    captured = _capture(app, row, user)

    if os.environ.get('CAPTURE_LLM_MESSAGES') == '1':
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, 'w', encoding='utf-8') as handle:
            json.dump(captured, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.write('\n')
        pytest.skip('fixture written')

    with open(FIXTURE, encoding='utf-8') as handle:
        expected = json.load(handle)

    assert sorted(captured) == sorted(expected)
    for mode in MODES:
        for call_name, messages in captured[mode].items():
            assert messages == expected[mode][call_name], (
                f'{call_name} messages changed with {mode}')


def test_the_capture_covers_both_layouts(recording):
    """Guards the fixture itself: the modes must differ where they should, so
    a capture taken with the flags ignored cannot pass for a valid one."""
    with open(FIXTURE, encoding='utf-8') as handle:
        expected = json.load(handle)

    off, on, marked = (expected['off'], expected['prefix_cache'],
                       expected['prefix_cache_with_breakpoints'])
    for call_name in ('title', 'summary', 'speaker_identification_contextual'):
        assert off[call_name][0]['content'] != on[call_name][0]['content'], call_name
    assert off['translation_segments'][0] != on['translation_segments'][0]
    for call_name in ('title', 'summary'):
        assert isinstance(marked[call_name][1]['content'], list), call_name
    assert isinstance(marked['translation_segments'][0][1]['content'], list)
    # The manual path has one layout, and breakpoints need the prefix cache.
    assert off['speaker_identification_manual'] == marked['speaker_identification_manual']
    assert expected['breakpoints_without_prefix_cache'] == off
