"""
Initial-prompt template management.

CRUD for user-defined, reusable transcription initial-prompt texts. Mirrors the
transcript/export/naming template blueprints: user-scoped, JSON in/out, a single
is_default per user, and a create-defaults seeding endpoint. Plain text only —
these are ASR hints, not summarization prompts, and carry no variables.
"""

from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from src.database import db
from src.models import InitialPromptTemplate

# Create blueprint
initial_prompt_templates_bp = Blueprint('initial_prompt_templates', __name__)


@initial_prompt_templates_bp.route('/api/initial-prompt-templates', methods=['GET'])
@login_required
def get_initial_prompt_templates():
    """Get all initial-prompt templates for the current user."""
    templates = InitialPromptTemplate.query.filter_by(user_id=current_user.id).all()
    return jsonify([template.to_dict() for template in templates])


@initial_prompt_templates_bp.route('/api/initial-prompt-templates', methods=['POST'])
@login_required
def create_initial_prompt_template():
    """Create a new transcription template (initial prompt and/or hotwords)."""
    data = request.json
    prompt = (data.get('template') or '') if data else ''
    hotwords = (data.get('hotwords') or '') if data else ''
    # A template needs a name and at least one of prompt / hotwords.
    if not data or not data.get('name') or not (prompt.strip() or hotwords.strip()):
        return jsonify({'error': 'Name and a prompt or hotwords are required'}), 400

    # If this is set as default, unset other defaults
    if data.get('is_default'):
        InitialPromptTemplate.query.filter_by(
            user_id=current_user.id,
            is_default=True
        ).update({'is_default': False})

    template = InitialPromptTemplate(
        user_id=current_user.id,
        name=data['name'],
        template=prompt,
        hotwords=hotwords or None,
        description=data.get('description'),
        is_default=data.get('is_default', False)
    )

    db.session.add(template)
    db.session.commit()

    return jsonify(template.to_dict()), 201


@initial_prompt_templates_bp.route('/api/initial-prompt-templates/<int:template_id>', methods=['PUT'])
@login_required
def update_initial_prompt_template(template_id):
    """Update an existing initial-prompt template."""
    template = InitialPromptTemplate.query.filter_by(
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
        InitialPromptTemplate.query.filter_by(
            user_id=current_user.id,
            is_default=True
        ).update({'is_default': False})

    template.name = data.get('name', template.name)
    template.template = data.get('template', template.template)
    if 'hotwords' in data:
        template.hotwords = data.get('hotwords') or None
    template.description = data.get('description', template.description)
    template.is_default = data.get('is_default', template.is_default)
    template.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify(template.to_dict())


@initial_prompt_templates_bp.route('/api/initial-prompt-templates/<int:template_id>', methods=['DELETE'])
@login_required
def delete_initial_prompt_template(template_id):
    """Delete an initial-prompt template."""
    template = InitialPromptTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    db.session.delete(template)
    db.session.commit()

    return jsonify({'success': True})


@initial_prompt_templates_bp.route('/api/initial-prompt-templates/create-defaults', methods=['POST'])
@login_required
def create_default_initial_prompt_templates():
    """Seed a few starter initial-prompt templates if the user has none."""
    existing = InitialPromptTemplate.query.filter_by(user_id=current_user.id).count()
    if existing > 0:
        return jsonify({'message': 'User already has templates'}), 200

    starters = [
        ('Business meeting',
         'This is a business meeting.',
         '',
         'Generic business meeting context'),
        ('Interview',
         'This is an interview between an interviewer and an interviewee.',
         '',
         'One-on-one interview context'),
        ('Podcast',
         'This is a podcast episode with hosts and guests having a conversation.',
         '',
         'Podcast / multi-speaker conversation context'),
        ('Lecture',
         'This is an academic lecture given by a single speaker.',
         '',
         'Single-speaker lecture or presentation context'),
    ]

    templates = []
    for idx, (name, text, hotwords, description) in enumerate(starters):
        templates.append(InitialPromptTemplate(
            user_id=current_user.id,
            name=name,
            template=text,
            hotwords=hotwords or None,
            description=description,
            is_default=(idx == 0),
        ))

    for template in templates:
        db.session.add(template)
    db.session.commit()

    return jsonify({
        'success': True,
        'templates': [template.to_dict() for template in templates]
    }), 201
