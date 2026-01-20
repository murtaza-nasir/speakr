"""
Calendar event extraction and export.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app, make_response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from src.database import db
from src.models import *
from src.utils import *
from src.services.calendar import generate_ics_content

# Create blueprint
events_bp = Blueprint('events', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None

def init_events_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


# --- Routes ---

@events_bp.route('/api/recording/<int:recording_id>/events', methods=['GET'])
@login_required
def get_recording_events(recording_id):
    """Get all events extracted from a recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if not has_recording_access(recording, current_user):
            return jsonify({'error': 'Unauthorized'}), 403

        events = Event.query.filter_by(recording_id=recording_id).all()
        return jsonify({'events': [event.to_dict() for event in events]})

    except Exception as e:
        current_app.logger.error(f"Error fetching events for recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


@events_bp.route('/api/event/<int:event_id>/ics', methods=['GET'])
@login_required
def download_event_ics(event_id):
    """Generate and download an ICS file for a single event."""
    try:
        event = db.session.get(Event, event_id)
        if not event:
            return jsonify({'error': 'Event not found'}), 404

        # Check permissions through recording access
        if not has_recording_access(event.recording, current_user):
            return jsonify({'error': 'Unauthorized'}), 403

        # Generate ICS content
        ics_content = generate_ics_content(event)

        # Create response with ICS file
        response = make_response(ics_content)
        response.headers['Content-Type'] = 'text/calendar; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename="{secure_filename(event.title)}.ics"'

        return response

    except Exception as e:
        current_app.logger.error(f"Error generating ICS for event {event_id}: {e}")
        return jsonify({'error': str(e)}), 500


@events_bp.route('/api/recording/<int:recording_id>/events/ics', methods=['GET'])
@login_required
def download_all_events_ics(recording_id):
    """Generate and download an ICS file containing all events from a recording."""
    try:
        recording = db.session.get(Recording, recording_id)
        if not recording:
            return jsonify({'error': 'Recording not found'}), 404

        if not has_recording_access(recording, current_user):
            return jsonify({'error': 'Unauthorized'}), 403

        # Get all events for this recording
        events = Event.query.filter_by(recording_id=recording_id).all()
        if not events:
            return jsonify({'error': 'No events found for this recording'}), 404

        # Generate combined ICS content
        ics_lines = []
        ics_lines.append("BEGIN:VCALENDAR")
        ics_lines.append("VERSION:2.0")
        ics_lines.append("PRODID:-//Speakr//Event Export//EN")
        ics_lines.append("CALSCALE:GREGORIAN")
        ics_lines.append("METHOD:PUBLISH")

        # Add each event
        for event in events:
            # Get the individual event's ICS content and extract just the VEVENT portion
            individual_ics = generate_ics_content(event)
            # Extract VEVENT block from individual ICS
            lines = individual_ics.split('\n')
            in_event = False
            for line in lines:
                if line.startswith('BEGIN:VEVENT'):
                    in_event = True
                if in_event:
                    ics_lines.append(line)
                if line.startswith('END:VEVENT'):
                    in_event = False

        ics_lines.append("END:VCALENDAR")
        ics_content = '\r\n'.join(ics_lines)

        # Create response with ICS file
        response = make_response(ics_content)
        response.headers['Content-Type'] = 'text/calendar; charset=utf-8'
        safe_title = secure_filename(recording.title) if recording.title else f'recording-{recording_id}'
        response.headers['Content-Disposition'] = f'attachment; filename="{safe_title}-events.ics"'

        return response

    except Exception as e:
        current_app.logger.error(f"Error generating ICS for all events in recording {recording_id}: {e}")
        return jsonify({'error': str(e)}), 500


@events_bp.route('/api/event/<int:event_id>', methods=['DELETE'])
@login_required
def delete_event(event_id):
    """Delete a single event."""
    try:
        event = db.session.get(Event, event_id)
        if not event:
            return jsonify({'error': 'Event not found'}), 404

        # Check permissions through recording access
        if not has_recording_access(event.recording, current_user):
            return jsonify({'error': 'Unauthorized'}), 403

        db.session.delete(event)
        db.session.commit()

        return jsonify({'success': True})

    except Exception as e:
        current_app.logger.error(f"Error deleting event {event_id}: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


