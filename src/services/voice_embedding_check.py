"""Detect that the transcription backend's voice embedding model has changed.

Voice profiles are stored as raw vectors with no record of what produced them.
If the backend starts returning embeddings from a different model, those
vectors live in a different space and stop matching, and the failure is
completely silent: `find_matching_speakers()` compares each stored profile in
turn and a mismatched pair raises, which it treats as a corrupt profile and
skips. The user sees voice matching quietly stop working, with nothing in the
logs to explain it (#380).

Dimension alone does not detect this. Two different models can both emit 256
dimensions, and nothing in Speakr enforces a dimension anyway: serialize is
`np.array(...).tobytes()`, deserialize is `np.frombuffer(...)`, and similarity
reshapes to (1, -1). All three work at any length.

The backend cannot be asked either. An ASR response carries the embeddings and
nothing about the model that made them; the connector knows which
*transcription* model it requested and never learns which *embedding* model the
service chose.

So Speakr asks the only question that has a reliable answer: it sends a fixed
clip and compares the embedding that comes back with the one stored when the
reference was taken. Measured against a whisperx-asr-service backend:

    same clip, same model, repeated       cosine 1.000000 (bit-identical)
    different voice, same model           cosine 0.270
    same voice, same words, 5s not 12s    cosine 0.516

A different model puts the clip in an unrelated space, so the separation is
wide and the threshold below has a lot of room. The last row is the reason the
clip is a committed fixture rather than anything generated: this comparison is
sensitive to the exact audio, so the reference is only meaningful against
byte-identical input.
"""

import hashlib
import json
import logging
import math
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# Bump this when the clip changes. A new version re-baselines every instance on
# upgrade rather than warning all of them, because a different clip cannot be
# compared with a reference taken from the old one.
CANARY_CLIP_VERSION = 'v1'
CANARY_FILENAME = f'voice_canary_{CANARY_CLIP_VERSION}.wav'
CANARY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'assets', CANARY_FILENAME)

# Same input through the same model measured exactly 1.0 here, and the nearest
# wrong answer measured 0.52. 0.98 leaves room for a backend whose GPU kernels
# are not bit-deterministic (that kind of noise stays above 0.9999) while
# staying far above anything that is genuinely a different model.
SIMILARITY_THRESHOLD = 0.98

SETTING_KEY = 'voice_embedding_reference'

STATUS_OK = 'ok'
STATUS_CHANGED = 'changed'
STATUS_UNKNOWN = 'unknown'


class CanaryUnavailable(Exception):
    """The probe could not be run, which is not the same as a mismatch."""


def cosine_similarity(a, b):
    """Plain cosine, no numpy, so this works without the embeddings extra."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return None
    return dot / (na * nb)


def backend_fingerprint():
    """Identify the configured backend well enough to notice reconfiguration.

    This is a TRIGGER, never the alarm. Renaming a host changes it without
    changing a single embedding, and a service that quietly upgrades its own
    model changes every embedding without touching it. Either way the probe
    decides; this only says when it is worth running one.
    """
    try:
        from src.services.transcription.registry import get_registry
        registry = get_registry()
        connector = registry.get_active_connector()
        parts = [
            registry.get_active_connector_name() or '',
            str(getattr(connector, 'base_url', '') or ''),
            str(getattr(connector, 'model', '') or ''),
        ]
    except Exception as e:
        logger.debug(f"Could not fingerprint the transcription backend: {e}")
        parts = ['unknown']
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]


def embeddings_supported():
    """Whether the active connector returns speaker embeddings at all."""
    try:
        from src.services.transcription.base import TranscriptionCapability
        from src.services.transcription.registry import get_registry
        connector = get_registry().get_active_connector()
        return bool(connector.supports(TranscriptionCapability.SPEAKER_EMBEDDINGS))
    except Exception as e:
        logger.debug(f"Could not determine speaker-embedding support: {e}")
        return False


def probe_backend():
    """Send the canary through the active connector and return its embedding.

    Raises CanaryUnavailable when the probe cannot be completed. That is
    deliberately distinct from a mismatch: a backend that is merely down must
    never be reported as a changed model.
    """
    from src.services.transcription.base import TranscriptionRequest
    from src.services.transcription.registry import get_registry

    if not os.path.exists(CANARY_PATH):
        raise CanaryUnavailable(f'canary clip missing at {CANARY_PATH}')

    try:
        connector = get_registry().get_active_connector()
    except Exception as e:
        raise CanaryUnavailable(f'no active transcription connector: {e}')

    try:
        with open(CANARY_PATH, 'rb') as f:
            response = connector.transcribe(TranscriptionRequest(
                audio_file=f,
                filename=CANARY_FILENAME,
                mime_type='audio/wav',
                language='en',
                diarize=True,
                min_speakers=1,
                max_speakers=1,
            ))
    except Exception as e:
        raise CanaryUnavailable(f'transcription of the canary failed: {e}')

    embeddings = getattr(response, 'speaker_embeddings', None) or {}
    if len(embeddings) != 1:
        # More than one speaker means the backend's diarizer split a
        # single-voice clip, so the result is not comparable with a reference
        # taken from one segment.
        raise CanaryUnavailable(
            f'expected exactly one speaker in the canary, got {len(embeddings)}')

    vector = list(embeddings.values())[0]
    if not vector:
        raise CanaryUnavailable('backend returned an empty embedding')
    return [float(x) for x in vector]


def load_reference():
    from src.models import SystemSetting
    raw = SystemSetting.get_setting(SETTING_KEY, None)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning('Stored voice embedding reference is not valid JSON; ignoring it')
        return None


def save_reference(data):
    from src.models import SystemSetting
    # set_setting commits for us.
    SystemSetting.set_setting(
        SETTING_KEY, json.dumps(data), setting_type='string',
        description='Reference embedding of the bundled canary clip, used to detect '
                    'that the transcription backend changed its voice embedding model')


def get_status(app=None):
    """What the admin UI shows. Never runs a probe."""
    reference = load_reference() or {}
    return {
        'status': reference.get('status', STATUS_UNKNOWN),
        'dimension': reference.get('dimension'),
        'clip_version': reference.get('clip_version'),
        'checked_at': reference.get('checked_at'),
        'similarity': reference.get('last_similarity'),
        'detail': reference.get('detail'),
        'supported': embeddings_supported(),
    }


def check_voice_embeddings(app, force=False):
    """Probe the backend and compare against the stored reference.

    Returns the same dict shape as get_status(). Never raises: a failure to
    check is reported, not thrown, because this runs on a startup thread.
    """
    if not embeddings_supported():
        logger.debug('Active connector returns no speaker embeddings; skipping the canary')
        return {'status': STATUS_UNKNOWN, 'supported': False,
                'detail': 'the active connector does not return speaker embeddings'}

    reference = load_reference()
    fingerprint = backend_fingerprint()

    # A stored reference from a different clip cannot be compared with this
    # one, so treat it as no reference and re-baseline.
    if reference and reference.get('clip_version') != CANARY_CLIP_VERSION:
        logger.info('Canary clip changed since the reference was taken; re-baselining')
        reference = None

    if reference and not force and reference.get('backend_fingerprint') == fingerprint:
        # Nothing about the configuration moved. The probe costs a real
        # transcription, so it is not run on every boot for no reason.
        logger.debug('Transcription backend unchanged since the last voice embedding check')
        return get_status(app)

    try:
        vector = probe_backend()
    except CanaryUnavailable as e:
        logger.warning(f'Voice embedding check could not run: {e}')
        # Leave any existing reference and its status untouched. A backend
        # that is down is not a backend that changed.
        status = get_status(app)
        status['detail'] = f'check could not run: {e}'
        return status

    now = datetime.utcnow().isoformat()

    if reference is None:
        save_reference({
            'clip_version': CANARY_CLIP_VERSION,
            'dimension': len(vector),
            'embedding': vector,
            'backend_fingerprint': fingerprint,
            'checked_at': now,
            'status': STATUS_OK,
            'last_similarity': 1.0,
            'detail': 'baseline recorded',
        })
        logger.info(f'Recorded a voice embedding baseline ({len(vector)} dimensions)')
        return get_status(app)

    stored = reference.get('embedding') or []
    if len(stored) != len(vector):
        detail = (f'the backend now returns {len(vector)}-dimensional embeddings, '
                  f'and stored voice profiles are {len(stored)}-dimensional')
        similarity = None
    else:
        similarity = cosine_similarity(stored, vector)
        detail = None

    changed = similarity is None or similarity < SIMILARITY_THRESHOLD
    if changed and detail is None:
        detail = (f'the same clip now embeds differently (similarity '
                  f'{similarity:.4f}, expected at least {SIMILARITY_THRESHOLD})')

    reference.update({
        'backend_fingerprint': fingerprint,
        'checked_at': now,
        'last_similarity': similarity,
        'status': STATUS_CHANGED if changed else STATUS_OK,
        'detail': detail if changed else None,
    })
    # The reference embedding itself is NOT replaced on a mismatch. It is what
    # the existing voice profiles were built against, so it stays until the
    # user rebuilds them or restores the old backend.
    save_reference(reference)

    if changed:
        _log_change_banner(app, detail)
        _raise_notification(app, detail)
    else:
        logger.info('Voice embedding check passed (similarity %.6f)', similarity)
        _clear_notification(app)

    return get_status(app)


def _log_change_banner(app, detail):
    """Say it loudly in the startup output.

    An admin who restarts after repointing their ASR endpoint is reading the
    container log at exactly that moment, and a single ERROR line among the
    ordinary startup chatter is easy to scroll past. This is the one place
    where a banner earns its width.
    """
    logger_ = getattr(app, 'logger', logger)
    width = 78
    lines = [
        '',
        '=' * width,
        'VOICE EMBEDDING MODEL CHANGED'.center(width),
        '=' * width,
        detail or 'the transcription backend returned an unexpected embedding',
        '',
        'Existing voice profiles were built by a different model and will not',
        'match new recordings. Voice matching will silently find nothing until',
        'one of the following is true:',
        '',
        '  * the previous transcription backend is restored, which clears this',
        '    by itself on the next check, or',
        '  * the affected voice profiles are rebuilt and the reference is reset',
        '    from the admin area.',
        '',
        'Set DISABLE_VOICE_EMBEDDING_CHECK=true to stop checking.',
        '=' * width,
        '',
    ]
    for line in lines:
        logger_.error(line)


def _raise_notification(app, detail):
    """Put the warning in front of every admin, not only in the logs."""
    try:
        from src.models import KIND_VOICE_EMBEDDING_CHANGED
        from src.services.notifications import notify
        notify(
            KIND_VOICE_EMBEDDING_CHANGED,
            'notifications.voiceEmbeddingChanged',
            admins=True,
            level='error',
            params={'detail': detail or ''},
            link='/admin',
        )
    except Exception as e:
        logger.warning('Could not raise the voice embedding notification: %s', e)


def _clear_notification(app):
    """The condition ended, so the notice goes away on its own.

    This is what makes restoring the previous backend a complete fix rather
    than something the admin also has to acknowledge.
    """
    try:
        from src.models import KIND_VOICE_EMBEDDING_CHANGED
        from src.services.notifications import resolve
        resolve(KIND_VOICE_EMBEDDING_CHANGED)
    except Exception as e:
        logger.warning('Could not clear the voice embedding notification: %s', e)


def rebaseline(app):
    """Accept the current backend as correct and discard the old reference.

    Called after the user rebuilds their voice profiles, or when they know the
    change was intentional.
    """
    from src.database import db
    from src.models import SystemSetting
    setting = SystemSetting.query.filter_by(key=SETTING_KEY).first()
    if setting:
        db.session.delete(setting)
        db.session.commit()
    return check_voice_embeddings(app, force=True)
