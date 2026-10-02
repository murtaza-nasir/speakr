"""
Event model for calendar events extracted from transcripts.

This module defines the Event model for storing calendar events
that are extracted from transcriptions.
"""

import json
import re
from datetime import datetime

from src.database import db

_ADDRESS = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


class Event(db.Model):
    """Calendar events extracted from transcripts."""

    id = db.Column(db.Integer, primary_key=True)
    recording_id = db.Column(db.Integer, db.ForeignKey('recording.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    start_datetime = db.Column(db.DateTime, nullable=False)
    end_datetime = db.Column(db.DateTime, nullable=True)
    location = db.Column(db.String(500), nullable=True)
    attendees = db.Column(db.Text, nullable=True)  # JSON list of attendees
    reminder_minutes = db.Column(db.Integer, nullable=True, default=15)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship
    recording = db.relationship('Recording', backref=db.backref('events', lazy=True, cascade='all, delete-orphan'))

    def to_dict(self):
        """Convert model to dictionary representation."""
        return {
            'id': self.id,
            'recording_id': self.recording_id,
            'title': self.title,
            'description': self.description,
            'start_datetime': self.start_datetime.isoformat() if self.start_datetime else None,
            'end_datetime': self.end_datetime.isoformat() if self.end_datetime else None,
            'location': self.location,
            'attendees': json.loads(self.attendees) if self.attendees else [],
            'reminder_minutes': self.reminder_minutes,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    def attendee_list(self):
        """Attendees as [{'name', 'email'}] (mailr spec G5).

        A string becomes a name, or an email when it looks like an address;
        an object keeps its name and email; anything else is dropped.
        """
        try:
            raw = json.loads(self.attendees) if self.attendees else []
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        people = []
        for item in raw:
            if isinstance(item, str):
                text = item.strip()
                if not text:
                    continue
                match = _ADDRESS.search(text)
                if match:
                    name = text.replace(match.group(0), '').strip(' <>()"\'') or None
                    people.append({'name': name, 'email': match.group(0)})
                else:
                    people.append({'name': text, 'email': None})
            elif isinstance(item, dict):
                name = item.get('name') if isinstance(item.get('name'), str) else None
                email = item.get('email') if isinstance(item.get('email'), str) else None
                if name or email:
                    people.append({'name': (name or '').strip() or None, 'email': (email or '').strip() or None})
        return people

    def api_dict(self):
        """The API v1 shape: to_dict with attendees as objects and floating.

        start_datetime and end_datetime are the wall-clock times the
        extraction produced, without a zone (floating: true).
        """
        data = self.to_dict()
        data['attendees'] = self.attendee_list()
        data['floating'] = True
        return data
