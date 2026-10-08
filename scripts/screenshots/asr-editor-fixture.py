"""Synthetic-only ASR editor fixture server; requires a disposable container."""
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
        sample_rate = options.get('audio_sample_rate', 16000)
        channels = options.get('audio_channels', 2)
        gain = options.get('audio_amplitude', 1)
        if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) or not .001 <= gain <= 1:
            return jsonify(error='Invalid disposable audio amplitude'), 400
        if sample_rate not in (8000, 16000, 44100, 48000) or channels not in (1, 2):
            return jsonify(error='Invalid disposable audio format'), 400
        frames = round(spectral_duration * sample_rate)
        format_suffix = ('' if (sample_rate, channels) == (16000, 2) else f'-{sample_rate}-{channels}') + (f'-quiet-{gain}' if gain != 1 else '')
        audio_path = str(fixture_dir / 'uploads' / f'spectral-fixture-{frames}{format_suffix}.wav')
        Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(audio_path).exists():
            period = bytearray()
            for i in range(sample_rate * 10):
                t = i / sample_rate
                frequency = 400 if t < 3 else 1200 if 4 <= t < 8 else 0
                amplitude = int(16000 * gain * math.sin(2 * math.pi * frequency * t)) if frequency else 0
                period += struct.pack('<hh', amplitude, -amplitude) if channels == 2 else struct.pack('<h', amplitude)
            with wave.open(audio_path, 'wb') as file:
                file.setnchannels(channels); file.setsampwidth(2); file.setframerate(sample_rate)
                for offset in range(0, frames, sample_rate * 10):
                    file.writeframes(period[:min(sample_rate * 10, frames-offset)*2*channels])
        if options.get('long'):
            segments[150]['end_time'] = 1640
        if options.get('video'):
            video_path = str(Path(audio_path).with_suffix('.mp4'))
            if not Path(video_path).exists():
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=320x180:r=10',
                                '-i', audio_path, '-t', str(spectral_duration), '-c:v', 'libx264',
                                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', video_path], check=True)
            audio_path = video_path
    recording = Recording(user_id=user.id, title='ASR split smoke fixture', status='COMPLETED',
                          audio_deleted_at=None if spectral else datetime.utcnow(), audio_path=audio_path,
                          audio_duration_seconds=spectral_duration if spectral else None,
                          mime_type='video/mp4' if options.get('video') else 'audio/wav',
                          speaker_embeddings=options.get('speaker_embeddings'),
                          speaker_label_map=options.get('speaker_label_map'),
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


@app.get('/__asr-segment-capture')
def segment_capture():
    file = fixture_dir / 'capture.json'
    return jsonify(json.loads(file.read_text()) if file.exists() else {})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8899, debug=False)
