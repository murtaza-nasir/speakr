"""External references: a link from a recording to an item in another system
(mailr spec G8), for example a mailr calendar event or conversation.

References are private per user: each user sees and writes only their own,
on recordings they can read.
"""

from datetime import datetime

from src.database import db


class RecordingExternalRef(db.Model):
    __tablename__ = 'recording_external_ref'

    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False)
    system = db.Column(db.String(40), nullable=False)
    kind = db.Column(db.String(40), nullable=False)
    ref = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), nullable=True)
    label = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('recording_id', 'user_id', 'system', 'kind', 'ref', name='uq_recording_external_ref'),
        db.Index('ix_external_ref_user_system_ref', 'user_id', 'system', 'ref'),
    )

    def to_dict(self):
        z = lambda v: v.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if v else None
        return {
            'id': self.id,
            'system': self.system,
            'kind': self.kind,
            'ref': self.ref,
            'url': self.url,
            'label': self.label,
            'created_at': z(self.created_at),
        }
