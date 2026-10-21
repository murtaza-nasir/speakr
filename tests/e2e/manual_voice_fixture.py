"""Owned synthetic browser fixture. Uses the real app, worker and ASR connector.

No production DB, recordings or external provider. The stand-in response proves
the upload/selection contract, not speech recognition or audible speech quality.
Run with --workdir NEW_DIRECTORY --port 8899 inside the isolated test runtime.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import wave

ROOT = Path(__file__).resolve().parents[2]


def configure(work):
    sys.path.insert(0, str(ROOT))
    for name in ('uploads', 'exports', 'instance', 'jobs'):
        (work / name).mkdir(parents=True, exist_ok=True)
    os.environ.update({
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{work}/instance/browser.db',
        'UPLOAD_FOLDER': str(work / 'uploads'), 'AUTO_EXPORT_DIR': str(work / 'exports'),
        'SECRET_KEY': 'manual-voice-local-fixture', 'ENABLE_AUTO_PROCESSING': 'false',
        'JOB_QUEUE_WORKERS': '0', 'SUMMARY_QUEUE_WORKERS': '0', 'RATELIMIT_ENABLED': 'false',
        'TEXT_MODEL_BASE_URL': 'http://127.0.0.1:19328/unused', 'TEXT_MODEL_NAME': '',
        'TEXT_MODEL_API_KEY': 'fixture', 'TRANSCRIPTION_API_KEY': 'fixture',
        'TRANSCRIPTION_CONNECTOR': 'asr_endpoint', 'ASR_BASE_URL': 'http://127.0.0.1:19328',
        'ASR_RETURN_SPEAKER_EMBEDDINGS': 'true', 'ASR_TIMEOUT': '30',
        'DISABLE_VOICE_EMBEDDING_CHECK': 'true', 'TRANSCRIPTION_MODEL': '',
        'ENABLE_INQUIRE_MODE': 'true', 'SMTP_HOST': '', 'EMBEDDING_BASE_URL': '',
        'VOICE_PROFILE_MIN_SPEECH_SECONDS': '15',
    })


def start_asr(work):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok","data":[]}')

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            mime = BytesParser(policy=default).parsebytes(
                f'Content-Type: {self.headers["Content-Type"]}\r\nMIME-Version: 1.0\r\n\r\n'.encode() + body)
            audio = next(p.get_payload(decode=True) for p in mime.iter_parts()
                         if p.get_param('name', header='Content-Disposition') == 'audio_file')
            with wave.open(io.BytesIO(audio)) as wav:
                duration = wav.getnframes() / wav.getframerate()
                frames, rate, channels = wav.getnframes(), wav.getframerate(), wav.getnchannels()
            item = {'sha256': hashlib.sha256(audio).hexdigest(), 'size': len(audio),
                    'duration': duration, 'frames': frames, 'rate': rate, 'channels': channels,
                    'query': parse_qs(urlsplit(self.path).query)}
            with (work / 'uploads.jsonl').open('a') as out:
                out.write(json.dumps(item) + '\n')
            (work / f'uploaded-{time.time_ns()}.wav').write_bytes(audio)
            mode = (work / 'provider-mode').read_text().strip() if (work / 'provider-mode').exists() else 'ok'
            if mode == 'slow':
                time.sleep(3)
            speakers = ['SPEAKER_00', 'SPEAKER_01'] if mode == 'multiple' else ['SPEAKER_00']
            end = min(1, duration) if mode == 'short' else duration
            result = {'text': 'Synthetic fixture', 'language': 'en',
                      'segments': [{'text': 'Synthetic fixture', 'speaker': s, 'start': 0, 'end': end}
                                   for s in speakers],
                      'speaker_embeddings': {s: [1.0, 0.0, 0.0] for s in speakers}}
            if mode == 'no-vector':
                result['speaker_embeddings'] = {}
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(('127.0.0.1', 19328), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def seed(work):
    from src.app import app, bcrypt
    from src.database import db
    from src.init_db import initialize_database
    from src.models import User, Speaker, Recording
    from src.services import voice_profiles as vp
    from src.services import voice_embedding_check as check
    path = work / 'uploads' / 'synthetic-selection.wav'
    # Distinct integer marker amplitudes; exact decoded samples can be compared.
    rate = 8000
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        wav.writeframes(b''.join(struct.pack('<h', (i * 31) % 24001 - 12000) for i in range(rate * 110)))
    with app.app_context():
        initialize_database(app)
        user = User(username='fixture', email='admin@example.com',
                    password=bcrypt.generate_password_hash('changeme').decode(),
                    email_verified=True, is_admin=True, ui_language='en')
        db.session.add(user)
        db.session.flush()
        speaker = Speaker(user_id=user.id, name='Fixture person', use_count=1)
        db.session.add(speaker)
        record = Recording(user_id=user.id, title='Synthetic range fixture', status='COMPLETED',
                           audio_path=str(path), original_filename=path.name, file_size=path.stat().st_size,
                           mime_type='audio/wav', audio_duration_seconds=110,
                           transcription=json.dumps([
                               {'speaker': 'SPEAKER_00', 'sentence': 'Keep this original draft', 'start_time': 0, 'end_time': 35},
                               {'speaker': 'SPEAKER_00', 'sentence': 'Independent selection', 'start_time': 35, 'end_time': 72.5},
                               {'speaker': 'SPEAKER_00', 'sentence': 'Selected fixture interval', 'start_time': 72.5, 'end_time': 94},
                               {'speaker': 'SPEAKER_00', 'sentence': 'Outside selection', 'start_time': 94, 'end_time': 110},
                           ]))
        db.session.add(record)
        db.session.commit()
        db.session.add(Recording(user_id=user.id, title='Second smoke fixture', status='COMPLETED',
            audio_path=str(path), original_filename=path.name, file_size=path.stat().st_size,
            mime_type='audio/wav', audio_duration_seconds=110, transcription='Second synthetic fixture'))
        db.session.commit()
        # Import/startup was disabled; install a known fixture reference without
        # a canary upload before serving the ordinary application.
        os.environ['DISABLE_VOICE_EMBEDDING_CHECK'] = 'false'
        space = vp.register_space([1, 0, 0], check.backend_fingerprint())
        check.save_reference({'status': 'ok', 'clip_version': check.CANARY_CLIP_VERSION,
                              'dimension': 3, 'embedding': [1, 0, 0],
                              'backend_fingerprint': check.backend_fingerprint(),
                              'checked_at': datetime.utcnow().isoformat()})
        (work / 'fixture.json').write_text(json.dumps({'recording_id': record.id, 'speaker_id': speaker.id,
            'space_id': space, 'audio_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'transcription': record.transcription}))


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--port', type=int, default=8899)
    parser.add_argument('--source', default=str(ROOT))
    args = parser.parse_args()
    ROOT = Path(args.source).resolve()
    work = Path(args.workdir).resolve()
    if work.exists():
        raise SystemExit('Use a new owned fixture directory')
    configure(work)
    server = start_asr(work)
    seed(work)
    # Same application, Gunicorn and browser entrypoint as run_speakr.py.
    try:
        subprocess.run([sys.executable, '-m', 'gunicorn', '--workers', '2', '--worker-class', 'gthread',
            '--threads', '4', '--bind', f'0.0.0.0:{args.port}', '--timeout', '600',
            '--chdir', str(ROOT), 'src.app:app'], check=True)
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
