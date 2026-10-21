"""Disposable dev server for the ASR split smoke; never imports a production DB."""
import json
import os
import math
import struct
import wave
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if os.environ.get('ASR_SPLIT_SMOKE') != '1':
    raise RuntimeError('Run only in a disposable container with ASR_SPLIT_SMOKE=1')

fixture_dir = Path('/tmp/speakr-asr-split')
fixture_dir.mkdir(parents=True, exist_ok=True)
copy_source = os.environ.get('ASR_WORKPLACE_COPY')
if copy_source:
    # Only the snapshot is read. All migrations and editor writes use a new /tmp DB.
    import sqlite3
    with sqlite3.connect(f'file:{copy_source}?mode=ro', uri=True) as source, sqlite3.connect(fixture_dir / 'test.db') as target:
        source.backup(target)
os.environ.update({
    'SQLALCHEMY_DATABASE_URI': f'sqlite:///{fixture_dir}/test.db',
    'UPLOAD_FOLDER': str(fixture_dir / 'uploads'),
    'SECRET_KEY': 'asr-split-disposable-test-only',
    'TEXT_MODEL_API_KEY': 'test-key', 'TRANSCRIPTION_API_KEY': 'test-key',
    'TRANSCRIPTION_BASE_URL': 'http://127.0.0.1:1/v1',
    'TEXT_MODEL_BASE_URL': 'http://127.0.0.1:1/v1',
    'ENABLE_AUTO_PROCESSING': 'false', 'ENABLE_AUTO_DELETION': 'false',
    'DISABLE_VOICE_EMBEDDING_CHECK': 'true', 'RATELIMIT_ENABLED': 'false',
    'JOB_QUEUE_WORKERS': '0', 'SUMMARY_QUEUE_WORKERS': '0',
    'WEBHOOK_GLOBAL_ENABLED': 'false', 'SESSION_COOKIE_SECURE': 'false',
})
if os.environ.get('ASR_SEGMENT_SMOKE') == '1':
    os.environ.update(TRANSCRIPTION_CONNECTOR='asr_endpoint', ASR_BASE_URL='http://127.0.0.1:9001',
                      TRANSCRIPTION_MODEL='large-v3', ASR_API_KEY='fixture-key')
    subprocess.Popen([sys.executable, str(Path(__file__).with_name('asr-segment-stub.py'))])

from flask import jsonify, request
from flask_login import login_user
from src.config import startup
startup.run_startup_tasks = lambda app: None
from src.app import app, csrf
from src.database import db
from src.models.user import User
from src.models.recording import Recording
from src.models import Speaker

app.jinja_env.auto_reload = True

@app.after_request
def identify_fixture_worker(response):
    response.headers['X-Synthetic-Worker'] = str(os.getpid())
    return response


@app.route('/__asr-split-fixture', methods=['POST'])
@csrf.exempt
def fixture():
    user = db.session.execute(db.select(User).filter_by(username='asr-split-smoke')).scalar_one_or_none()
    if user is None:
        user = User(username='asr-split-smoke', email='asr-split@example.invalid',
                    ui_language='ru', email_verified=True)
        db.session.add(user)
        db.session.flush()
    options = request.get_json(silent=True) or {}
    for name in options.get('database_speakers', []):
        if not Speaker.query.filter_by(user_id=user.id, name=name).first():
            db.session.add(Speaker(user_id=user.id, name=name))
    if os.environ.get('ASR_SEGMENT_SMOKE') == '1':
        (fixture_dir / 'mode.txt').write_text(options.get('asr_mode', 'normal'))
        from src.models import SystemSetting
        SystemSetting.set_setting('transcription_default_model', 'large-v3')
    user.editor_autosave = bool(options.get('autosave'))
    segments = [dict(speaker='Анна', start_time=i * 10, end_time=i * 10 + 8,
                     sentence=f'Первая фраза {i}. Ответ другого человека {i}.') for i in range(int(options.get('count', 220)))]
    if options.get('empty'):
        segments = []
    if options.get('segments') is not None:
        segments = options['segments']
    spectral = bool(options.get('spectrogram'))
    spectral_duration = float(options.get('audio_duration', 2200))
    if not math.isfinite(spectral_duration) or not .25 <= spectral_duration <= 2200:
        return jsonify(error='Invalid disposable audio duration'), 400
    audio_path = None
    if spectral:
        frames = round(spectral_duration * 16000)
        audio_path = str(fixture_dir / 'uploads' / f'spectral-fixture-{frames}.wav')
        Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(audio_path).exists():
            period = bytearray()
            for i in range(160000):
                t = i / 16000
                frequency = 400 if t < 3 else 1200 if 4 <= t < 8 else 0
                amplitude = int(16000 * math.sin(2 * math.pi * frequency * t)) if frequency else 0
                period += struct.pack('<hh', amplitude, -amplitude)
            with wave.open(audio_path, 'wb') as file:
                file.setnchannels(2); file.setsampwidth(2); file.setframerate(16000)
                for offset in range(0, frames, 160000):
                    file.writeframes(period[:min(160000, frames-offset)*4])
        if options.get('long'):
            segments[150]['end_time'] = 1640
    recording = Recording(user_id=user.id, title='ASR split smoke fixture', status='COMPLETED',
                          audio_deleted_at=None if spectral else datetime.utcnow(), audio_path=audio_path,
                          audio_duration_seconds=spectral_duration if spectral else None,
                          transcription=json.dumps(segments, ensure_ascii=False))
    db.session.add(recording)
    db.session.flush()
    recording.title = f'ASR split smoke fixture {recording.id}'
    db.session.commit()
    login_user(user)
    return jsonify(id=recording.id, title=recording.title, isolated=True)


@app.route('/__asr-split-ready')
def ready():
    return jsonify(isolated=True, fixture='asr-segment-split')


@app.get('/__asr-spectrum-counters')
def spectrum_counters():
    from src.services.segment_spectrogram import cache_root
    file = cache_root() / 'state.json'
    state = json.loads(file.read_text()) if file.exists() else {}
    return jsonify(isolated=True, renderCount=state.get('renderCount', 0))


@app.post('/__asr-workplace-copy')
@csrf.exempt
def copied_recording():
    if not copy_source:
        return jsonify(error='No copied database'), 404
    recording = db.session.get(Recording, 33)
    if recording is None:
        return jsonify(error='Source recording missing'), 404
    user = db.session.get(User, recording.user_id)
    user.ui_language = 'ru'
    user.editor_autosave = False
    db.session.commit()
    login_user(user)
    return jsonify(id=recording.id, title=recording.title, isolated=True, copied_database=True)


@app.get('/__asr-segment-capture')
def segment_capture():
    file = fixture_dir / 'capture.json'
    return jsonify(json.loads(file.read_text()) if file.exists() else {})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8899, debug=False)
