"""Files uploaded to be joined into one recording.

The upload dialog can join several files into one recording. Each file goes
through the normal upload pipeline (size limits, conversion, duplicate check)
and is stored as a part here instead of as a recording, so nothing that is
not yet a recording can appear in lists, search or statistics. When every
part of a group has arrived, one recording is created and the merge job
joins the parts' audio (see services/upload_join.py).

A part's row outlives its media by a day, so a retried upload of a part whose
group was already joined is answered with the joined recording.
"""

from datetime import datetime

from src.database import db


class UploadJoinPart(db.Model):
    __tablename__ = 'upload_join_part'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'group_id', 'part_index', name='uq_upload_join_part'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    group_id = db.Column(db.String(64), nullable=False, index=True)
    part_index = db.Column(db.Integer, nullable=False)   # 0-based position in the joined recording
    part_count = db.Column(db.Integer, nullable=False)
    audio_path = db.Column(db.String(500), nullable=True)  # storage locator; None once joined
    original_filename = db.Column(db.String(500), nullable=True)
    file_size = db.Column(db.BigInteger, nullable=True)
    mime_type = db.Column(db.String(100), nullable=True)
    audio_duration_seconds = db.Column(db.Float, nullable=True)
    meeting_date = db.Column(db.DateTime, nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)  # set once, when the group is joined
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
