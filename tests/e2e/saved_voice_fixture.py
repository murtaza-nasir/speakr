"""Private isolated fixture: durable samples seeded without ASR."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import struct
import sys
import uuid

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--workdir', required=True)
parser.add_argument('--port', type=int, default=18899)
parser.add_argument('--resume', action='store_true')
parser.add_argument('--purge-previews', action='store_true')
parser.add_argument('--plain', action='store_true')
args = parser.parse_args()
sys.path.insert(0, args.source)
sys.path.insert(0, str(Path(args.source) / 'tests/e2e'))
import manual_voice_fixture as fixture
fixture.ROOT = Path(args.source)
work = Path(args.workdir)
if work.exists() and not args.resume:
    raise SystemExit('new owned fixture required')
fixture.configure(work)
if args.purge_previews:
    assert args.resume and (work / 'fixture.json').is_file(), 'owned existing fixture required'
    from src.app import app
    from src.services.manual_voice_playback import get_jobs
    with app.app_context():
        print('Owned playback reservations remaining:', get_jobs().cleanup(all_jobs=True))
    raise SystemExit(0)
server = fixture.start_asr(work)
if not args.resume:
    fixture.seed(work)
from src.app import app
from src.database import db
from src.models import ManualVoiceSample
from src.services import voice_profiles as vp
with app.app_context():
    data = json.loads((work / 'fixture.json').read_text())
    if args.resume:
        assert data.get('sample_ids'), 'owned saved sample fixture required'
    from src.models import Speaker, Recording
    speaker = db.session.get(Speaker, data['speaker_id'])
    record = db.session.get(Recording, data['recording_id'])
    if args.plain and not args.resume:
        record.transcription='Plain source without timestamps'
        data['transcription']=record.transcription
    ids = []
    for start, end in ([] if args.resume else [(70000, 94000), (35000, 55000)]):
        row = ManualVoiceSample(id=str(uuid.uuid4()), user_id=speaker.user_id, speaker_id=speaker.id,
            recording_id=record.id, start_ms=start, end_ms=end, speech_ms=end-start,
            source_audio_sha256=data['audio_sha256'], preparation_id=str(uuid.uuid4()),
            space_id=data['space_id'], embedding=struct.pack('<3f', 1, 0, 0), dimension=3)
        db.session.add(row)
        ids.append(row.id)
    db.session.flush()
    vp.refresh_speaker_summary(speaker)
    db.session.commit()
    if not args.resume:
        data['sample_ids'] = ids
    (work / 'fixture.json').write_text(json.dumps(data))
try:
    subprocess.run([sys.executable, '-m', 'gunicorn', '--workers', '2', '--worker-class', 'gthread',
        '--threads', '4', '--bind', f'0.0.0.0:{args.port}', '--timeout', '600', '--chdir', args.source,
        'src.app:app'], check=True)
finally:
    server.shutdown()
