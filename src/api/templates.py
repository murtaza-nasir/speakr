"""
Transcript template management.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
import time
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from src.database import db
from src.models import *
from src.utils import *

# Create blueprint
templates_bp = Blueprint('templates', __name__)

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

def init_templates_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


# --- Routes ---

@templates_bp.route('/api/transcript-templates', methods=['GET'])
@login_required
def get_transcript_templates():
    """Get all transcript templates for the current user."""
    templates = TranscriptTemplate.query.filter_by(user_id=current_user.id).all()
    return jsonify([template.to_dict() for template in templates])



@templates_bp.route('/api/transcript-templates', methods=['POST'])
@login_required
def create_transcript_template():
    """Create a new transcript template."""
    data = request.json
    if not data or not data.get('name') or not data.get('template'):
        return jsonify({'error': 'Name and template are required'}), 400

    # If this is set as default, unset other defaults
    if data.get('is_default'):
        TranscriptTemplate.query.filter_by(
            user_id=current_user.id,
            is_default=True
        ).update({'is_default': False})

    template = TranscriptTemplate(
        user_id=current_user.id,
        name=data['name'],
        template=data['template'],
        description=data.get('description'),
        is_default=data.get('is_default', False)
    )

    db.session.add(template)
    db.session.commit()

    return jsonify(template.to_dict()), 201



@templates_bp.route('/api/transcript-templates/<int:template_id>', methods=['PUT'])
@login_required
def update_transcript_template(template_id):
    """Update an existing transcript template."""
    template = TranscriptTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # If this is set as default, unset other defaults
    if data.get('is_default'):
        TranscriptTemplate.query.filter_by(
            user_id=current_user.id,
            is_default=True
        ).update({'is_default': False})

    template.name = data.get('name', template.name)
    template.template = data.get('template', template.template)
    template.description = data.get('description', template.description)
    template.is_default = data.get('is_default', template.is_default)
    template.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify(template.to_dict())



@templates_bp.route('/api/transcript-templates/<int:template_id>', methods=['DELETE'])
@login_required
def delete_transcript_template(template_id):
    """Delete a transcript template."""
    template = TranscriptTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    db.session.delete(template)
    db.session.commit()

    return jsonify({'success': True})



@templates_bp.route('/api/transcript-templates/create-defaults', methods=['POST'])
@login_required
def create_default_templates():
    """Create default templates for the user if they don't have any."""
    existing_templates = TranscriptTemplate.query.filter_by(user_id=current_user.id).count()

    if existing_templates > 0:
        return jsonify({'message': 'User already has templates'}), 200

    templates = []

    # Default template 1: Simple conversation
    template1 = TranscriptTemplate(
        user_id=current_user.id,
        name="Simple Conversation",
        template="{{speaker}}: {{text}}",
        description="Clean format with just speaker names and text",
        is_default=True
    )
    templates.append(template1)

    # Default template 2: Timestamped format
    template2 = TranscriptTemplate(
        user_id=current_user.id,
        name="Timestamped",
        template="[{{start_time}} - {{end_time}}] {{speaker}}: {{text}}",
        description="Format with timestamps and speaker names",
        is_default=False
    )
    templates.append(template2)

    # Default template 3: Interview Q&A
    template3 = TranscriptTemplate(
        user_id=current_user.id,
        name="Interview Q&A",
        template="{{speaker|upper}}:\n{{text}}\n",
        description="Interview format with speaker names in uppercase",
        is_default=False
    )
    templates.append(template3)

    # Default template 4: Meeting Minutes
    template4 = TranscriptTemplate(
        user_id=current_user.id,
        name="Meeting Minutes",
        template="• [{{start_time}}] {{speaker}}: {{text}}",
        description="Bulleted format ideal for meeting notes",
        is_default=False
    )
    templates.append(template4)

    # Default template 5: Court Transcript
    template5 = TranscriptTemplate(
        user_id=current_user.id,
        name="Court Transcript",
        template="{{index}}    {{speaker|upper}}: {{text}}",
        description="Legal deposition format with line numbers",
        is_default=False
    )
    templates.append(template5)

    # Default template 6: SRT Subtitle
    template6 = TranscriptTemplate(
        user_id=current_user.id,
        name="SRT Subtitle",
        template="{{index}}\n{{start_time|srt}} --> {{end_time|srt}}\n{{text}}\n",
        description="SRT subtitle format for video editing",
        is_default=False
    )
    templates.append(template6)

    # Default template 7: Screenplay
    template7 = TranscriptTemplate(
        user_id=current_user.id,
        name="Screenplay",
        template="                    {{speaker|upper}}\n        {{text}}\n",
        description="Film script format with centered names",
        is_default=False
    )
    templates.append(template7)

    # Add all templates to database
    for template in templates:
        db.session.add(template)

    db.session.commit()

    return jsonify({
        'success': True,
        'templates': [template.to_dict() for template in templates]
    }), 201



