"""Voice profile storage: embedding spaces and per-person voice samples.

VoiceEmbeddingSpace (Stage 2): the transcription backend never says which
model produced an embedding, so each model Speakr has seen is identified by
the embedding it returns for the bundled canary clip (see
services/voice_embedding_check.py). Embeddings are only compared within one
space. Rows written before spaces existed carry no space and belong to the
"legacy" space, the first one registered, so upgrading changes nothing.

SpeakerVoiceSample (Stage 3): one normalized embedding per (recording,
diarization label) assigned to a person. A person's voice variants are
computed from these samples on the fly, so removing a wrongly assigned
sample undoes its effect completely.
"""

from datetime import datetime
import uuid

from src.database import db


class VoiceEmbeddingSpace(db.Model):
    __tablename__ = 'voice_embedding_space'

    id = db.Column(db.Integer, primary_key=True)
    dimension = db.Column(db.Integer, nullable=True)
    canary_embedding = db.Column(db.JSON, nullable=True)  # the canary clip's embedding in this space
    backend_fingerprint = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'dimension': self.dimension,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_seen_at': self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


class SpeakerVoiceSample(db.Model):
    __tablename__ = 'speaker_voice_sample'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    speaker_id = db.Column(db.Integer, db.ForeignKey('speaker.id', ondelete='CASCADE'), nullable=False, index=True)
    # NULL for the sample that carries a pre-existing averaged profile, and
    # after the source recording is deleted (the voiceprint stays, as the
    # average always did).
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='SET NULL'), nullable=True, index=True)
    label = db.Column(db.String(100), nullable=True)  # diarization label within the recording, e.g. SPEAKER_01
    embedding = db.Column(db.LargeBinary, nullable=False)  # L2-normalized float32
    dimension = db.Column(db.Integer, nullable=False)
    space_id = db.Column(db.Integer, db.ForeignKey('voice_embedding_space.id', ondelete='SET NULL'), nullable=True, index=True)
    speech_seconds = db.Column(db.Float, nullable=True)
    source = db.Column(db.String(20), nullable=False, default='confirmed')  # confirmed | auto | legacy
    weight = db.Column(db.Float, nullable=False, default=1.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    speaker = db.relationship('Speaker', backref=db.backref('voice_samples', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'speaker_id': self.speaker_id,
            'recording_id': self.recording_id,
            'label': self.label,
            'dimension': self.dimension,
            'space_id': self.space_id,
            'speech_seconds': self.speech_seconds,
            'source': self.source,
            'weight': self.weight,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ManualVoiceSample(db.Model):
    """One explicitly confirmed, independently selected PCM range."""
    __tablename__ = 'manual_voice_sample'
    __table_args__ = (
        db.CheckConstraint('start_ms >= 0 AND end_ms > start_ms', name='manual_voice_range'),
        db.CheckConstraint('speech_ms > 0 AND speech_ms <= end_ms - start_ms', name='manual_voice_speech'),
    )

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    speaker_id = db.Column(db.Integer, db.ForeignKey('speaker.id', ondelete='CASCADE'), nullable=False, index=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='SET NULL'), nullable=True, index=True)
    start_ms = db.Column(db.Integer, nullable=False)
    end_ms = db.Column(db.Integer, nullable=False)
    source_audio_sha256 = db.Column(db.String(64), nullable=False)
    preparation_id = db.Column(db.String(36), nullable=False, unique=True)
    space_id = db.Column(db.Integer, db.ForeignKey('voice_embedding_space.id'), nullable=False, index=True)
    embedding = db.Column(db.LargeBinary, nullable=False)
    dimension = db.Column(db.Integer, nullable=False)
    speech_ms = db.Column(db.Integer, nullable=False)
    source = db.Column(db.String(20), nullable=False, default='manual-range')
    weight = db.Column(db.Float, nullable=False, default=1.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    speaker = db.relationship('Speaker', backref=db.backref('manual_voice_samples', lazy=True, cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('manual_voice_samples', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id, 'speaker_id': self.speaker_id, 'recording_id': self.recording_id,
            'start_ms': self.start_ms, 'end_ms': self.end_ms,
            'duration_ms': self.end_ms - self.start_ms, 'speech_ms': self.speech_ms,
            'space_id': self.space_id, 'source': self.source,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ManualVoiceSampleReceipt(db.Model):
    """Bounded consumed-job identity; survives clear, sample deletion and merge."""
    __tablename__ = 'manual_voice_sample_receipt'

    preparation_id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    # Initial target identity deliberately has no speaker/sample FK cascade.
    initial_speaker_id = db.Column(db.Integer, nullable=False)
    initial_speaker_created_at = db.Column(db.DateTime, nullable=False)
    sample_id = db.Column(db.String(36), nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    user = db.relationship('User', backref=db.backref('manual_voice_sample_receipts', lazy=True, cascade='all, delete-orphan'))
