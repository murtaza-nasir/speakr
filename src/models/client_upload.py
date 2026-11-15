"""Uploads a browser named, so that an aborted one can be discarded.

The upload form may carry a client_upload_id. When the browser aborts the
request after the body was sent, the server still finishes the ingestion and
would leave a recording nobody asked for. POST /upload/cancel marks the id
here; the ingestion looks the mark up before it queues the first job, and the
cancel looks up the recording an ingestion has already stored
(src/services/upload_cancel.py). Rows are removed after a day.
"""

from datetime import datetime

from src.database import db


class ClientUpload(db.Model):
    __tablename__ = 'client_upload'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'client_upload_id', name='uq_client_upload'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    client_upload_id = db.Column(db.String(80), nullable=False)
    # The recording the ingestion stored, and its created_at: SQLite gives
    # the id of a deleted recording to a later one (#505), and that one is
    # never this upload's.
    recording_id = db.Column(db.Integer, nullable=True)
    recording_created_at = db.Column(db.DateTime, nullable=True)
    canceled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
