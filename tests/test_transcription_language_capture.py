"""The detected transcription language is recorded on the recording (#386).

`TranscriptionResponse.language` has carried the service's detected language
since the connector architecture landed, and every connector that can report
one populates it. Nothing consumed it, so it was discarded after each run.

The webhook payload had documented a `language` field since #275 by reading
`recording.transcription_language`, a column that was never added, so the value
was always None and the payload's None-filter dropped the key. Subscribers have
never received it. These tests cover the column that closes that gap, and the
distinction that makes it correct: the language belongs to the AUDIO, recorded
once at transcription time, not to the user's live preference.
"""

import os

import pytest

from src.app import app
from src.database import db
from src.models import Recording, User
from src.utils.language import normalize_language_code


@pytest.fixture
def owner():
    with app.app_context():
        user = User(username=f'lang_owner_{os.getpid()}',
                    email=f'lang_owner_{os.getpid()}@example.com', password='x')
        db.session.add(user)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        Recording.query.filter_by(user_id=uid).delete()
        User.query.filter_by(id=uid).delete()
        db.session.commit()


def test_the_column_exists_and_is_distinct_from_the_user_preference():
    """User.transcription_language is what the user ASKED for and can change at
    any time. Recording.transcription_language is what the service reported for
    that audio. Conflating them would make old recordings report today's
    setting, which is why the webhook could not simply read the user's."""
    recording_cols = {c.name for c in Recording.__table__.columns}
    user_cols = {c.name for c in User.__table__.columns}
    assert 'transcription_language' in recording_cols
    assert 'transcription_language' in user_cols, 'the user preference should still exist'


def test_it_round_trips_through_the_database_and_the_api(owner):
    with app.app_context():
        rec = Recording(user_id=owner, title='Nederlandse vergadering',
                        audio_path='/tmp/x.wav', transcription_language='nl')
        db.session.add(rec)
        db.session.commit()

        stored = db.session.get(Recording, rec.id)
        assert stored.transcription_language == 'nl'
        assert stored.to_dict()['transcription_language'] == 'nl'


def test_it_is_nullable_for_recordings_that_predate_it(owner):
    """Existing rows stay NULL. The audio would have to be re-transcribed to
    learn the language, so the field is simply absent rather than guessed."""
    with app.app_context():
        rec = Recording(user_id=owner, title='Older recording', audio_path='/tmp/x.wav')
        db.session.add(rec)
        db.session.commit()
        assert db.session.get(Recording, rec.id).transcription_language is None
        assert db.session.get(Recording, rec.id).to_dict()['transcription_language'] is None


@pytest.mark.parametrize('reported,expected', [
    ('en', 'en'),
    ('EN', 'en'),
    ('en-US', 'en'),          # locale codes are stripped to the language
    ('English', 'en'),        # some backends report a display name
    ('nl', 'nl'),
    (None, None),
    ('', None),
    ('auto', None),           # not a language; means the backend detected nothing
    ('gibberish', None),      # unrecognised, and better absent than wrong
])
def test_what_the_backend_reports_is_normalised_before_storage(reported, expected):
    """Backends disagree on the form: 'en', 'EN', 'en-US', 'English'. Storing
    them raw would make the field useless to a subscriber trying to route on
    it, and normalisation yields None rather than raising on anything odd."""
    assert normalize_language_code(reported) == expected


def test_the_stored_value_fits_the_column(owner):
    """VARCHAR(20) against normalised output, which is a 2-letter code."""
    length = Recording.__table__.columns['transcription_language'].type.length
    assert length >= 10
    for candidate in ('en', 'nl', 'zh', 'pt'):
        assert len(normalize_language_code(candidate) or '') <= length


def test_the_transcription_task_assigns_it_from_the_response():
    """Pins the wiring, not just the column. Both storage paths in
    transcribe_with_connector, chunked and single-file, must set it, or a
    large file would silently lose the language a small one records."""
    import ast
    import inspect as _inspect
    from src.tasks import processing

    source = _inspect.getsource(processing.transcribe_with_connector)
    tree = ast.parse(source.lstrip())

    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == 'transcription_language'
                for t in node.targets)
    ]
    assert len(assignments) == 2, (
        f'expected the single-file and chunked paths each to record the language, '
        f'found {len(assignments)} assignment(s)')

    # Each must go through the normaliser rather than storing the raw value.
    for node in assignments:
        call = node.value
        assert isinstance(call, ast.Call), 'the reported language must be normalised'
        assert getattr(call.func, 'id', None) == 'normalize_language_code'
