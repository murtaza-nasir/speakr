"""Recordings for the browser tests: three finished recordings of the admin.

    python tests/e2e/seed_recordings.py        # with Speakr's settings in the environment

run_speakr.py runs this after it has created the database and the admin, so
an instance started for the browser tests has recordings to open: without an
ASR service nothing uploaded there would ever get a transcript. Each
recording gets a short WAV file (a tone in bursts where the transcript has
speech) in UPLOAD_FOLDER and a transcript with two unnamed speakers. Running it again adds nothing.

test_asr_editor_ui.py finds them by title (TITLES below); the smoke test's
recording checks use them as well. test_trim_ui.py cuts the third one
(TRIM_TITLE), which no other test uses.
"""

import json
import math
import os
import struct
import sys
import wave
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# The second one is created later, so it is the first in the list.
TITLES = ("Browser test: long transcript", "Browser test: meeting")
# The trim test (test_trim_ui.py) cuts this one, so the others keep their audio.
# It is the oldest, so it is the last in the list.
TRIM_TITLE = "Browser test: trim"
AUDIO_SECONDS = 90

SENTENCES = (
    "Good morning everyone and thanks for joining.",
    "Morning. Shall we start with the budget?",
    "Yes, the first item is the budget for next year.",
    "I have the numbers here, travel is up by ten percent.",
    "That is more than we planned for.",
    "Two conferences moved to spring, that explains most of it.",
    "Fine. Can you send the breakdown after the call?",
    "I will send it this afternoon.",
    "The second item is the new hire for the lab.",
    "We have three candidates and interviews are next week.",
    "Good. Anything else before we close?",
    "Nothing from my side, thank you.",
)


def meeting_segments():
    """Twelve segments, two alternating unnamed speakers, six seconds each."""
    return [
        {"speaker": f"SPEAKER_0{i % 2}", "sentence": text, "start_time": i * 6.0, "end_time": i * 6.0 + 5.5}
        for i, text in enumerate(SENTENCES)
    ]


def write_tone(path, seconds=AUDIO_SECONDS, rate=8000):
    """A mono 16-bit WAV shaped like the meeting: a 220 Hz tone in word-length
    bursts while a segment of meeting_segments() lasts, silence between the
    segments and after the last one. The waveform of the editor's timeline
    then shows the segments, and has pauses to snap to."""
    spoken = len(SENTENCES) * 6.0
    frames = bytearray()
    for n in range(seconds * rate):
        t = n / rate
        in_segment = t < spoken and t % 6.0 < 5.5
        word = int((t % 6.0) / 0.45)
        in_word = (t % 6.0) % 0.45 < 0.35
        level = 3000 + 900 * ((word * 7) % 5) if in_segment and in_word else 0
        frames += struct.pack("<h", int(level * math.sin(2 * math.pi * 220 * n / rate)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def seed():
    from src.app import app, db
    from src.models import Recording, User

    with app.app_context():
        admin = User.query.filter_by(is_admin=True).order_by(User.id).first()
        if admin is None:
            raise SystemExit("seed_recordings.py: no admin user; create it first")
        uploads = app.config.get("UPLOAD_FOLDER") or os.environ.get("UPLOAD_FOLDER")
        os.makedirs(uploads, exist_ok=True)
        transcript = json.dumps(meeting_segments())
        created = []
        now = datetime.utcnow()
        for order, title in enumerate(TITLES + (TRIM_TITLE,)):
            if Recording.query.filter_by(user_id=admin.id, title=title).first():
                continue
            audio = os.path.join(uploads, f"browser-test-{order}.wav")
            write_tone(audio)
            minutes = 60 if title == TRIM_TITLE else 10 * (len(TITLES) - order)
            when = now - timedelta(minutes=minutes)
            recording = Recording(
                user_id=admin.id, title=title, status="COMPLETED", transcription=transcript,
                audio_path=audio, original_filename=os.path.basename(audio), mime_type="audio/wav",
                file_size=os.path.getsize(audio), audio_duration_seconds=float(AUDIO_SECONDS),
                created_at=when, meeting_date=when, completed_at=when, is_inbox=False,
                summary="A short budget and hiring meeting, seeded for the browser tests.",
            )
            db.session.add(recording)
            created.append(title)
        db.session.commit()
        print(f"seed_recordings.py: created {created or 'nothing (already there)'}")


if __name__ == "__main__":
    seed()
