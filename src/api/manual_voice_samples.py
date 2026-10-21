"""Session/CSRF protected manual ranges, separate from automatic sample IDs."""
from functools import wraps
import os
from werkzeug.exceptions import RequestedRangeNotSatisfiable
from flask import Blueprint, current_app, jsonify, request, send_file
from flask_login import current_user, login_required

from src.database import db
from src.services import manual_voice_samples as service

manual_voice_samples_bp = Blueprint('manual_voice_samples', __name__)


@manual_voice_samples_bp.record_once
def start_private_range_maintenance(state):
    # db.init_app precedes blueprint registration. No provider, source upload,
    # or table access is needed to purge expired private files on idle restart.
    with state.app.app_context():
        service.manual_jobs.start_maintenance(state.app)
        from src.services.manual_voice_playback import get_jobs
        get_jobs()


def guarded(function):
    @wraps(function)
    def handle(*args, **kwargs):
        try:
            response = function(*args, **kwargs)
            if function.__name__ != 'prepare':
                service.purge_expired_receipts(current_user.id)
                db.session.commit()
            return response
        except service.ManualVoiceError as error:
            db.session.rollback()
            if error.code in ('expired', 'consumed'):
                service.purge_expired_receipts(current_user.id)
                db.session.commit()
            response = jsonify(code=service.safe_code(error.code), **({'check_url': '/admin#voice-embeddings',
                'check_endpoint': '/admin/voice-embeddings/check'} if error.code in ('unavailable', 'space') else {}))
            response.headers['Cache-Control'] = 'private, no-store'
            return response, error.status
        except Exception:
            db.session.rollback()
            # No provider response, exception text, paths or credentials enter logs.
            current_app.logger.warning('Manual voice range request failed')
            return jsonify(code='unavailable'), 503
    return handle


def access(recording_id, edit=True):
    from src.models import Recording
    from src.api.recordings import has_recording_access
    recording = Recording.query.filter_by(id=recording_id).populate_existing().first()
    if recording is None:
        raise service.ManualVoiceError('missing', 404)
    if not has_recording_access(recording, current_user, require_edit=edit):
        raise service.ManualVoiceError('forbidden', 403)
    return recording


def local_path(recording):
    from src.services.storage import get_storage_service
    if not recording.audio_path or recording.audio_deleted_at:
        raise service.ManualVoiceError('missing', 404)
    storage = get_storage_service()
    locator = storage.parse_locator(recording.audio_path)
    if not locator or not locator.is_local:
        raise service.ManualVoiceError('remote', 501)
    return str(storage.resolve_local_filesystem_path(recording.audio_path))


def owned_profile(speaker_id, lock=False):
    from src.models import Speaker
    if lock:
        speaker = service.begin_profile_transaction(speaker_id, current_user.id)
    else:
        speaker = Speaker.query.filter_by(id=speaker_id, user_id=current_user.id).first()
    if speaker is None:
        raise service.ManualVoiceError('missing', 404)
    return speaker


def private_json(payload, status=200):
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'private, no-store'
    return response, status


@manual_voice_samples_bp.route('/recordings/<int:recording_id>/manual_voice_samples/prepare', methods=['POST'])
@login_required
@guarded
def prepare(recording_id):
    from src.services.transcription import get_registry
    from src.services.voice_embedding_check import default_transcription_model
    recording = access(recording_id)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get('speaker_id')) is not int:
        raise service.ManualVoiceError('bounds', 400)
    speaker = owned_profile(data['speaker_id'], lock=True)
    if db.session.connection().dialect.name == 'postgresql':
        from src.models import Recording
        Recording.query.filter_by(id=recording_id).with_for_update().populate_existing().first()
    recording = access(recording_id)
    minimum = service.minimum_speech()
    start, end = service.validate_bounds(data.get('start_ms'), data.get('end_ms'), minimum)
    space = service.space_snapshot()
    registry = get_registry()
    connector = registry.get_active_connector()
    # Current configured model, with the recording's historical language hints.
    params = {'transcription_model': default_transcription_model(),
              'language': recording.transcription_language or None,
              'hotwords': recording.resolved_hotwords or None,
              'initial_prompt': recording.resolved_initial_prompt or None}
    result = service.manual_jobs.start(current_app._get_current_object(), current_user.id, recording.id,
        speaker, local_path(recording), start, end, registry.get_active_connector_name(), connector.config, params, space)
    db.session.commit()  # Release the profile lock, preparation made no durable profile writes.
    return private_json(result, 202)


def check_meta(meta, recording_id=None, speaker_id=None, lock_source=False):
    if meta['user_id'] != current_user.id or (recording_id is not None and meta['recording_id'] != recording_id):
        raise service.ManualVoiceError('missing', 404)
    if speaker_id is not None and meta['speaker_id'] != speaker_id:
        raise service.ManualVoiceError('missing', 404)
    speaker = owned_profile(meta['speaker_id'])
    if speaker.created_at.isoformat() != meta['speaker_created_at']:
        raise service.ManualVoiceError('missing', 404)
    if lock_source and db.session.connection().dialect.name == 'postgresql':
        from src.models import Recording
        Recording.query.filter_by(id=meta['recording_id']).with_for_update().populate_existing().first()
    recording = access(meta['recording_id'])
    return local_path(recording)


@manual_voice_samples_bp.route('/recordings/<int:recording_id>/manual_voice_samples/preparations/<job_id>', methods=['GET', 'DELETE'])
@login_required
@guarded
def preparation(recording_id, job_id):
    access(recording_id)
    with service.manual_jobs.locked(job_id) as (_, snapshot):
        if snapshot['user_id'] != current_user.id or snapshot['recording_id'] != recording_id:
            raise service.ManualVoiceError('missing', 404)
    owned_profile(snapshot['speaker_id'], lock=True)
    # Access and identity are checked outside the job lock, after the DB lock.
    path = check_meta(snapshot, recording_id)
    with service.manual_jobs.locked(job_id) as (directory, meta):
        if request.method == 'DELETE':
            service.manual_jobs._cancel(directory, meta)
        else:
            try:
                if path != meta['path'] or service.source_signature(path) != meta['signature']:
                    raise service.ManualVoiceError('changed', 409)
                service.check_space(meta['space'])
            except (OSError, service.ManualVoiceError) as error:
                service.manual_jobs._cancel(directory, meta)
                meta['state'], meta['code'] = 'failed', error.code if isinstance(error, service.ManualVoiceError) else 'changed'
                service.manual_jobs._write(directory, meta)
        result = service.manual_jobs.public(meta)
    db.session.commit()
    return private_json(result)


@manual_voice_samples_bp.route('/recordings/<int:recording_id>/manual_voice_samples/preparations/<job_id>/audio', methods=['GET'])
@login_required
@guarded
def audio(recording_id, job_id):
    access(recording_id)
    with service.manual_jobs.locked(job_id) as (_, snapshot):
        if snapshot['user_id'] != current_user.id or snapshot['recording_id'] != recording_id:
            raise service.ManualVoiceError('missing', 404)
    owned_profile(snapshot['speaker_id'], lock=True)
    path = check_meta(snapshot, recording_id)
    with service.manual_jobs.locked(job_id) as (directory, meta):
        if meta['state'] != 'ready':
            raise service.ManualVoiceError('not_ready', 409)
        service.check_space(meta['space'])
        result = meta['result']
        if service.source_digest(path) != (result['source_audio_sha256'], result['source_size']):
            service.manual_jobs._cancel(directory, meta)
            raise service.ManualVoiceError('changed', 409)
        clip = directory / 'work' / 'clip.wav'
        if clip.is_symlink() or clip.stat().st_size > service.MAX_CLIP_BYTES:
            raise service.ManualVoiceError('size', 413)
        # Open while locked; send_file owns the fd even if cancellation unlinks the path.
        stream = clip.open('rb')
        size = os.fstat(stream.fileno()).st_size
        if size > service.MAX_CLIP_BYTES:
            stream.close()
            raise service.ManualVoiceError('size', 413)
        response = send_file(stream, mimetype='audio/wav', download_name='voice-range.wav',
                             conditional=False, etag=False, max_age=0)
        # A private file object intentionally carries no filesystem path. Tell
        # native media players its actual finite size, and serve authenticated
        # byte ranges from the same opened descriptor (never reopen a path).
        response.content_length = size
        try:
            response.make_conditional(request.environ, accept_ranges=True, complete_length=size)
        except RequestedRangeNotSatisfiable:
            stream.close()
            raise service.ManualVoiceError('bounds', 416)
        response.headers['Cache-Control'] = 'private, no-store'
    db.session.commit()
    return response


@manual_voice_samples_bp.route('/speakers/<int:speaker_id>/manual_voice_samples', methods=['GET', 'POST'])
@login_required
@guarded
def samples(speaker_id):
    from src.models import ManualVoiceSample, Recording
    owned_profile(speaker_id)
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'job_id'} or not isinstance(data['job_id'], str):
            raise service.ManualVoiceError('bounds', 400)
        sample, created = service.commit_sample(current_user.id, speaker_id, data['job_id'],
                                               lambda meta: check_meta(meta, speaker_id=speaker_id, lock_source=True))
        return private_json({'id': sample.id, 'sample_id': sample.id, 'created': created}, 201 if created else 200)
    output = []
    for sample in ManualVoiceSample.query.filter_by(user_id=current_user.id, speaker_id=speaker_id).order_by(ManualVoiceSample.created_at).all():
        item = {'id': sample.id, 'source': 'manual-range', 'start_ms': sample.start_ms, 'end_ms': sample.end_ms,
                'speech_ms': sample.speech_ms, 'space_id': sample.space_id, 'recording_id': None,
                'recording_title': None, 'source_available': False, 'source_deleted': sample.recording_id is None}
        if sample.recording_id is not None:
            recording = db.session.get(Recording, sample.recording_id)
            from src.api.recordings import has_recording_access
            if recording and has_recording_access(recording, current_user):
                item.update(recording_id=recording.id, recording_title=recording.title,
                            source_available=bool(recording.audio_path and not recording.audio_deleted_at))
        output.append(item)
    return private_json({'samples': output})


@manual_voice_samples_bp.route('/speakers/<int:speaker_id>/manual_voice_samples/<sample_id>', methods=['DELETE'])
@login_required
@guarded
def delete_sample(speaker_id, sample_id):
    from src.models import ManualVoiceSample
    from src.services import voice_profiles as vp
    speaker = owned_profile(speaker_id, lock=True)
    sample = ManualVoiceSample.query.filter_by(id=sample_id, speaker_id=speaker_id, user_id=current_user.id).first()
    if sample is None:
        raise service.ManualVoiceError('missing', 404)
    db.session.delete(sample)
    db.session.flush()
    vp.refresh_speaker_summary(speaker)
    db.session.commit()
    vp._calibration_cache.clear()
    return private_json({'deleted': True, 'summary': vp.voice_summary(speaker), 'speaker': speaker.to_dict()})


def playback_source(speaker_id, sample_id):
    from src.models import ManualVoiceSample
    owned_profile(speaker_id)
    sample = ManualVoiceSample.query.filter_by(id=sample_id, speaker_id=speaker_id, user_id=current_user.id).first()
    if sample is None or sample.recording_id is None:
        raise service.ManualVoiceError('missing', 404)
    recording = access(sample.recording_id, edit=False)
    path = local_path(recording)
    identity = dict(user_id=current_user.id, speaker_id=speaker_id, sample_id=sample.id,
        recording_id=recording.id, start_ms=sample.start_ms, end_ms=sample.end_ms,
        digest=sample.source_audio_sha256)
    return sample, recording, path, identity


def playback_guard(function):
    @wraps(function)
    def handle(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except service.ManualVoiceError as error:
            return private_json({'code': service.safe_code(error.code)}, error.status)
        except RequestedRangeNotSatisfiable:
            return private_json({'code': 'bounds'}, 416)
        except Exception:
            current_app.logger.warning('Saved voice playback unavailable')
            return private_json({'code': 'unavailable'}, 503)
    return handle


@manual_voice_samples_bp.route('/speakers/<int:speaker_id>/manual_voice_samples/<sample_id>/source')
@login_required
@playback_guard
def saved_source(speaker_id, sample_id):
    sample, recording, _, identity = playback_source(speaker_id, sample_id)
    return private_json({'recording_id': recording.id, 'recording_title': recording.title,
        'sample_id': sample.id, 'speaker_id': speaker_id, 'start_ms': sample.start_ms, 'end_ms': sample.end_ms})


@manual_voice_samples_bp.route('/speakers/<int:speaker_id>/manual_voice_samples/<sample_id>/playback', methods=['POST'])
@login_required
@playback_guard
def start_playback(speaker_id, sample_id):
    from src.services.manual_voice_playback import get_jobs
    if request.get_json(silent=True) not in (None, {}):
        raise service.ManualVoiceError('bounds', 400)
    _, _, path, identity = playback_source(speaker_id, sample_id)
    return private_json(get_jobs().start(identity, path), 202)


@manual_voice_samples_bp.route('/speakers/<int:speaker_id>/manual_voice_samples/<sample_id>/playbacks/<preview_id>', methods=['GET', 'DELETE'])
@manual_voice_samples_bp.route('/speakers/<int:speaker_id>/manual_voice_samples/<sample_id>/playbacks/<preview_id>/audio')
@login_required
@playback_guard
def saved_playback(speaker_id, sample_id, preview_id):
    from src.services.manual_voice_playback import get_jobs
    _, _, path, identity = playback_source(speaker_id, sample_id)
    jobs = get_jobs()
    is_audio = request.path.endswith('/audio')
    meta, resource = jobs.read(identity, preview_id, request.args.get('lease'), path,
        release=request.method == 'DELETE', open_audio=is_audio)
    if is_audio:
        size = os.fstat(resource.fileno()).st_size
        response = send_file(resource, mimetype='audio/wav', download_name='voice-range.wav', conditional=False, etag=False, max_age=0)
        response.content_length = size
        try:
            response.make_conditional(request.environ, accept_ranges=True, complete_length=size)
        except RequestedRangeNotSatisfiable:
            resource.close()
            raise
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    return private_json(jobs.public(meta))
