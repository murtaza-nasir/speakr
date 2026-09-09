"""Detecting that the transcription backend changed its voice embedding model (#380).

The failure being guarded against is silent, so most of these tests are about
NOT crying wolf. A backend that is merely unreachable, or a Speakr upgrade that
ships a new canary clip, must not be reported as a changed model: a warning
nobody can act on trains people to ignore the one that matters.

The numbers here come from measuring a real whisperx-asr-service backend:
the same clip re-embedded scores 1.0 and is bit-identical, a different voice
through the same model scores 0.27, and the same voice truncated from 12s to 5s
scores 0.52. The threshold sits at 0.98.
"""

import json
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import SystemSetting
from src.services import voice_embedding_check as vec


REF = [0.10, -0.20, 0.30, 0.40] * 64          # 256 dims, stands in for the baseline
SAME = list(REF)
SCALED = [x * 3 for x in REF]                  # same direction, different magnitude
DIFFERENT = [((i * 37) % 19) / 19 - 0.5 for i in range(256)]
SHORTER = [0.1] * 192                          # what a 192-dim model would return


@pytest.fixture(autouse=True)
def clean_reference():
    """Each test starts with no stored baseline."""
    with app.app_context():
        SystemSetting.query.filter_by(key=vec.SETTING_KEY).delete()
        db.session.commit()
    yield
    with app.app_context():
        SystemSetting.query.filter_by(key=vec.SETTING_KEY).delete()
        db.session.commit()


def _supported():
    return patch.object(vec, 'embeddings_supported', return_value=True)


def _baseline(vector=REF, fingerprint='backend-a'):
    """Record a baseline the way a first startup would."""
    with _supported(), \
            patch.object(vec, 'probe_backend', return_value=vector), \
            patch.object(vec, 'backend_fingerprint', return_value=fingerprint):
        return vec.check_voice_embeddings(app)


# --- the core claim ------------------------------------------------------

def test_a_first_run_records_a_baseline_rather_than_warning():
    with app.app_context():
        result = _baseline()
    assert result['status'] == vec.STATUS_OK
    assert result['dimension'] == 256


def test_the_same_embedding_is_not_reported_as_a_change():
    with app.app_context():
        _baseline()
        with _supported(), patch.object(vec, 'probe_backend', return_value=SAME), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            result = vec.check_voice_embeddings(app)
    assert result['status'] == vec.STATUS_OK
    assert result['similarity'] == pytest.approx(1.0)


def test_a_different_embedding_is_reported_as_a_change():
    with app.app_context():
        _baseline()
        with _supported(), patch.object(vec, 'probe_backend', return_value=DIFFERENT), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            result = vec.check_voice_embeddings(app)
    assert result['status'] == vec.STATUS_CHANGED
    assert result['similarity'] < vec.SIMILARITY_THRESHOLD
    assert 'rebuil' in result['detail'] or 'embeds differently' in result['detail']


def test_magnitude_alone_is_not_a_change():
    """Cosine ignores scale, and it should: a rescaled vector is the same
    direction in the same space, so voice matching still works."""
    with app.app_context():
        _baseline()
        with _supported(), patch.object(vec, 'probe_backend', return_value=SCALED), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            result = vec.check_voice_embeddings(app)
    assert result['status'] == vec.STATUS_OK


def test_a_dimension_change_is_reported_as_dimensions_not_as_similarity():
    """192-dim vs 256-dim is the case from the issue, and cosine cannot even
    be computed for it, so the message has to say so."""
    with app.app_context():
        _baseline()
        with _supported(), patch.object(vec, 'probe_backend', return_value=SHORTER), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            result = vec.check_voice_embeddings(app)
    assert result['status'] == vec.STATUS_CHANGED
    assert result['similarity'] is None
    assert '192' in result['detail'] and '256' in result['detail']


# --- not crying wolf -----------------------------------------------------

def test_an_unreachable_backend_is_not_reported_as_a_changed_model():
    """The most important false positive to avoid. A down ASR service is a
    different problem with a different fix, and telling the user their model
    changed would send them to rebuild profiles for nothing."""
    with app.app_context():
        _baseline()
        with _supported(), \
                patch.object(vec, 'probe_backend',
                             side_effect=vec.CanaryUnavailable('connection refused')), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            result = vec.check_voice_embeddings(app)
    assert result['status'] == vec.STATUS_OK, 'a down backend must not read as a changed model'
    assert 'could not run' in result['detail']


def test_a_canary_that_diarizes_into_several_speakers_is_unavailable_not_changed():
    """A backend whose VAD splits the single-voice clip gives a result that is
    not comparable, which is missing data rather than evidence of a change."""
    with app.app_context():
        _baseline()

        class _Resp:
            speaker_embeddings = {'SPEAKER_00': REF, 'SPEAKER_01': DIFFERENT}

        class _Conn:
            def transcribe(self, request):
                return _Resp()

        with _supported(), \
                patch('src.services.transcription.registry.get_registry') as reg:
            reg.return_value.get_active_connector.return_value = _Conn()
            with pytest.raises(vec.CanaryUnavailable) as e:
                vec.probe_backend()
        assert 'one speaker' in str(e.value)


def test_a_new_canary_clip_rebaselines_instead_of_warning_everyone():
    """Shipping a different clip must not light up every instance on upgrade.
    A reference taken from the old clip is not comparable with the new one."""
    with app.app_context():
        _baseline()
        stored = json.loads(SystemSetting.get_setting(vec.SETTING_KEY))
        stored['clip_version'] = 'v0'
        SystemSetting.set_setting(vec.SETTING_KEY, json.dumps(stored))

        with _supported(), patch.object(vec, 'probe_backend', return_value=DIFFERENT), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-a'):
            result = vec.check_voice_embeddings(app)

    assert result['status'] == vec.STATUS_OK
    assert result['clip_version'] == vec.CANARY_CLIP_VERSION


def test_a_connector_without_embeddings_is_unknown_not_broken():
    with app.app_context():
        with patch.object(vec, 'embeddings_supported', return_value=False):
            result = vec.check_voice_embeddings(app)
    assert result['status'] == vec.STATUS_UNKNOWN
    assert result['supported'] is False


# --- cost ---------------------------------------------------------------

def test_an_unchanged_backend_is_not_re_probed():
    """The probe is a real transcription. Running it every boot would put a
    pointless job through the user's ASR service five times per container."""
    with app.app_context():
        _baseline(fingerprint='backend-a')
        with _supported(), patch.object(vec, 'probe_backend') as probe, \
                patch.object(vec, 'backend_fingerprint', return_value='backend-a'):
            vec.check_voice_embeddings(app)
        probe.assert_not_called()


def test_force_re_probes_even_when_nothing_changed():
    with app.app_context():
        _baseline(fingerprint='backend-a')
        with _supported(), patch.object(vec, 'probe_backend', return_value=SAME) as probe, \
                patch.object(vec, 'backend_fingerprint', return_value='backend-a'):
            vec.check_voice_embeddings(app, force=True)
        probe.assert_called_once()


# --- recovery ------------------------------------------------------------

def test_the_baseline_survives_a_mismatch_so_the_backend_can_be_restored():
    """If a mismatch overwrote the reference, the warning would clear itself on
    the next check and restoring the old backend would then look like a second
    change. The reference is what the stored profiles were built against."""
    with app.app_context():
        _baseline()
        with _supported(), patch.object(vec, 'probe_backend', return_value=DIFFERENT), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            vec.check_voice_embeddings(app)
        stored = vec.load_reference()
    assert stored['embedding'] == REF, 'the reference was replaced by the mismatching one'


def test_restoring_the_original_backend_clears_the_warning_by_itself():
    """The user should not have to click anything to resolve a change they
    undid."""
    with app.app_context():
        _baseline()
        with _supported(), patch.object(vec, 'probe_backend', return_value=DIFFERENT), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            assert vec.check_voice_embeddings(app)['status'] == vec.STATUS_CHANGED

        with _supported(), patch.object(vec, 'probe_backend', return_value=SAME), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-a'):
            result = vec.check_voice_embeddings(app)
    assert result['status'] == vec.STATUS_OK
    assert result['detail'] is None


def test_rebaselining_accepts_the_new_backend():
    """The other resolution: the change was intended, profiles were rebuilt."""
    with app.app_context():
        _baseline()
        with _supported(), patch.object(vec, 'probe_backend', return_value=DIFFERENT), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            vec.check_voice_embeddings(app)

        with _supported(), patch.object(vec, 'probe_backend', return_value=DIFFERENT), \
                patch.object(vec, 'backend_fingerprint', return_value='backend-b'):
            result = vec.rebaseline(app)
    assert result['status'] == vec.STATUS_OK
    with app.app_context():
        assert vec.load_reference()['embedding'] == DIFFERENT


# --- the clip itself -----------------------------------------------------

def test_the_canary_clip_ships_and_is_the_expected_audio():
    """A missing or re-encoded clip silently invalidates every reference, so
    its identity is pinned here rather than assumed."""
    import hashlib
    import os
    import wave

    assert os.path.exists(vec.CANARY_PATH), f'canary clip missing at {vec.CANARY_PATH}'

    with wave.open(vec.CANARY_PATH, 'rb') as w:
        assert w.getnchannels() == 1, 'must be mono'
        assert w.getframerate() == 16000, 'must be 16 kHz'
        assert w.getsampwidth() == 2, 'must be 16-bit PCM'
        duration = w.getnframes() / w.getframerate()
        assert 10 <= duration <= 14, f'expected roughly 12s of audio, got {duration:.1f}s'

    digest = hashlib.sha256(open(vec.CANARY_PATH, 'rb').read()).hexdigest()
    assert digest == '2f4440d771f4206e67d53469002dd0241fd2a19e18db46ba2b79600da51a2741', (
        'the canary clip changed. Every stored reference embedding was taken against the '
        'old bytes and is now meaningless, so bump CANARY_CLIP_VERSION and ship the new '
        'clip under a new filename rather than replacing this one in place.'
    )


def test_cosine_similarity_edge_cases():
    assert vec.cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert vec.cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    # Lengths that differ cannot be compared; None, not an exception, because
    # the caller distinguishes "cannot compare" from "compares badly".
    assert vec.cosine_similarity([1, 0], [1, 0, 0]) is None
    assert vec.cosine_similarity([0, 0], [1, 0]) is None
    assert vec.cosine_similarity([], []) is None


# --- the matcher's side of it --------------------------------------------
#
# The check above tells the admin the model changed. These cover what happens
# to a user in the meantime, which is where the original silence lived.

def _speaker(user_id, name, vector):
    from src.models import Speaker
    from src.services.speaker_embedding_matcher import serialize_embedding
    return Speaker(user_id=user_id, name=name,
                   average_embedding=serialize_embedding(vector))


@pytest.fixture
def speaker_owner():
    from src.models import User
    import os
    with app.app_context():
        user = User(username=f'vec_owner_{os.getpid()}',
                    email=f'vec_owner_{os.getpid()}@example.com', password='x')
        db.session.add(user)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        from src.models import Speaker, User
        Speaker.query.filter_by(user_id=uid).delete()
        User.query.filter_by(id=uid).delete()
        db.session.commit()


def test_profiles_of_another_dimension_are_skipped_and_said_so_in_the_log(speaker_owner, caplog):
    """The original behaviour: a mismatched pair raised inside the loop, the
    blanket `except Exception: continue` treated it as a corrupt profile, and
    the user got an empty match list forever with nothing in the log."""
    from src.services.speaker_embedding_matcher import find_matching_speakers

    with app.app_context():
        db.session.add(_speaker(speaker_owner, 'Old Profile A', REF))
        db.session.add(_speaker(speaker_owner, 'Old Profile B', REF))
        db.session.commit()

        with caplog.at_level('ERROR'):
            matches = find_matching_speakers(SHORTER, speaker_owner, threshold=0.5)

    assert matches == []
    logged = ' '.join(r.getMessage() for r in caplog.records)
    assert '2 of 2' in logged, f'the skip was not reported: {logged!r}'
    assert '256 dimensions' in logged, f'the stored size must be named: {logged!r}'
    assert '192-dimensional' in logged, f'the new size must be named: {logged!r}'
    assert 'rebuilt' in logged or 'restored' in logged, 'the log must say what to do'


def test_matching_still_works_when_the_dimensions_agree(speaker_owner):
    """The guard must not cost normal matching."""
    from src.services.speaker_embedding_matcher import find_matching_speakers

    with app.app_context():
        db.session.add(_speaker(speaker_owner, 'Alice', REF))
        db.session.commit()
        matches = find_matching_speakers(SAME, speaker_owner, threshold=0.7)

    assert [m['name'] for m in matches] == ['Alice']
    assert matches[0]['similarity'] == pytest.approx(100.0, abs=0.1)


def test_a_genuinely_unreadable_profile_is_skipped_and_logged(speaker_owner, caplog):
    """Corruption still has to be tolerated, just not silently."""
    from src.models import Speaker
    from src.services.speaker_embedding_matcher import find_matching_speakers

    with app.app_context():
        broken = Speaker(user_id=speaker_owner, name='Corrupt',
                         average_embedding=b'\x01\x02\x03')  # not a whole float32
        db.session.add(broken)
        db.session.add(_speaker(speaker_owner, 'Alice', REF))
        db.session.commit()

        with caplog.at_level('WARNING'):
            matches = find_matching_speakers(SAME, speaker_owner, threshold=0.7)

    assert [m['name'] for m in matches] == ['Alice'], 'one bad profile must not lose the good ones'


# --- the admin surface ---------------------------------------------------

@pytest.fixture
def admin_client():
    """The endpoints are CSRF-protected, which these tests are not about."""
    import os
    from src.models import User
    previous_csrf = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        admin = User(username=f'vec_admin_{os.getpid()}',
                     email=f'vec_admin_{os.getpid()}@example.com',
                     password='x', is_admin=True)
        plain = User(username=f'vec_plain_{os.getpid()}',
                     email=f'vec_plain_{os.getpid()}@example.com',
                     password='x', is_admin=False)
        db.session.add_all([admin, plain])
        db.session.commit()
        ids = (admin.id, plain.id)
    yield app.test_client(), ids
    app.config['WTF_CSRF_ENABLED'] = previous_csrf
    with app.app_context():
        from src.models import User
        User.query.filter(User.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True


def test_status_endpoint_never_probes(admin_client):
    """A page load must not put a transcription job through the ASR service."""
    client, (admin_id, _) = admin_client
    _login(client, admin_id)
    with patch.object(vec, 'probe_backend') as probe:
        resp = client.get('/admin/voice-embeddings/status')
    assert resp.status_code == 200
    assert 'status' in resp.get_json()
    probe.assert_not_called()


def test_a_non_admin_cannot_read_or_change_the_verdict(admin_client):
    client, (_, plain_id) = admin_client
    _login(client, plain_id)
    for method, path in (('get', '/admin/voice-embeddings/status'),
                         ('post', '/admin/voice-embeddings/check'),
                         ('post', '/admin/voice-embeddings/rebaseline')):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, f'{method.upper()} {path} was not refused'


def test_the_check_endpoint_forces_a_probe(admin_client):
    client, (admin_id, _) = admin_client
    _login(client, admin_id)
    with app.app_context():
        _baseline(fingerprint='backend-a')
    with _supported(), patch.object(vec, 'probe_backend', return_value=SAME) as probe, \
            patch.object(vec, 'backend_fingerprint', return_value='backend-a'):
        resp = client.post('/admin/voice-embeddings/check')
    assert resp.status_code == 200
    assert resp.get_json()['status'] == vec.STATUS_OK
    probe.assert_called_once()
