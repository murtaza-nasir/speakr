"""
Naming template management.

This blueprint handles CRUD operations for naming templates,
which define how recording titles are generated from filenames,
metadata, and AI-generated content.
"""

import json
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from src.database import db
from src.models import NamingTemplate

# Create blueprint
naming_templates_bp = Blueprint('naming_templates', __name__)


# --- Routes ---

@naming_templates_bp.route('/api/naming-templates', methods=['GET'])
@login_required
def get_naming_templates():
    """Get all naming templates for the current user."""
    templates = NamingTemplate.query.filter_by(user_id=current_user.id).all()
    return jsonify([template.to_dict() for template in templates])


@naming_templates_bp.route('/api/naming-templates', methods=['POST'])
@login_required
def create_naming_template():
    """Create a new naming template."""
    data = request.json
    if not data or not data.get('name') or not data.get('template'):
        return jsonify({'error': 'Name and template are required'}), 400

    # Validate regex patterns if provided
    regex_patterns = data.get('regex_patterns', {})
    if regex_patterns:
        if not isinstance(regex_patterns, dict):
            return jsonify({'error': 'regex_patterns must be a dictionary'}), 400
        # Validate each regex pattern
        import re
        for var_name, pattern in regex_patterns.items():
            try:
                re.compile(pattern)
            except re.error as e:
                return jsonify({'error': f'Invalid regex pattern for "{var_name}": {str(e)}'}), 400

    # If this is set as default, unset other defaults
    if data.get('is_default'):
        NamingTemplate.query.filter_by(
            user_id=current_user.id,
            is_default=True
        ).update({'is_default': False})

    template = NamingTemplate(
        user_id=current_user.id,
        name=data['name'],
        template=data['template'],
        description=data.get('description'),
        regex_patterns=json.dumps(regex_patterns) if regex_patterns else None,
        is_default=data.get('is_default', False)
    )

    db.session.add(template)
    db.session.commit()

    return jsonify(template.to_dict()), 201


@naming_templates_bp.route('/api/naming-templates/<int:template_id>', methods=['GET'])
@login_required
def get_naming_template(template_id):
    """Get a specific naming template."""
    template = NamingTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    return jsonify(template.to_dict())


@naming_templates_bp.route('/api/naming-templates/<int:template_id>', methods=['PUT'])
@login_required
def update_naming_template(template_id):
    """Update an existing naming template."""
    template = NamingTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    # Validate regex patterns if provided
    if 'regex_patterns' in data:
        regex_patterns = data['regex_patterns']
        if regex_patterns:
            if not isinstance(regex_patterns, dict):
                return jsonify({'error': 'regex_patterns must be a dictionary'}), 400
            import re
            for var_name, pattern in regex_patterns.items():
                try:
                    re.compile(pattern)
                except re.error as e:
                    return jsonify({'error': f'Invalid regex pattern for "{var_name}": {str(e)}'}), 400

    # If this is set as default, unset other defaults
    if data.get('is_default'):
        NamingTemplate.query.filter_by(
            user_id=current_user.id,
            is_default=True
        ).update({'is_default': False})

    template.name = data.get('name', template.name)
    template.template = data.get('template', template.template)
    template.description = data.get('description', template.description)
    template.is_default = data.get('is_default', template.is_default)

    if 'regex_patterns' in data:
        regex_patterns = data['regex_patterns']
        template.regex_patterns = json.dumps(regex_patterns) if regex_patterns else None

    template.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify(template.to_dict())


@naming_templates_bp.route('/api/naming-templates/<int:template_id>', methods=['DELETE'])
@login_required
def delete_naming_template(template_id):
    """Delete a naming template."""
    template = NamingTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    # Check if any tags are using this template
    from src.models import Tag
    tags_using = Tag.query.filter_by(naming_template_id=template_id).count()
    if tags_using > 0:
        return jsonify({
            'error': f'Cannot delete template: {tags_using} tag(s) are using this template'
        }), 400

    db.session.delete(template)
    db.session.commit()

    return jsonify({'success': True})


@naming_templates_bp.route('/api/naming-templates/create-defaults', methods=['POST'])
@login_required
def create_default_naming_templates():
    """Create default naming templates for the user if they don't have any."""
    existing_templates = NamingTemplate.query.filter_by(user_id=current_user.id).count()

    if existing_templates > 0:
        return jsonify({'message': 'User already has naming templates'}), 200

    templates = []

    # Default template 1: AI Title Only (default)
    template1 = NamingTemplate(
        user_id=current_user.id,
        name="AI Title Only",
        template="{{ai_title}}",
        description="Uses AI-generated title from transcription content",
        is_default=True
    )
    templates.append(template1)

    # Default template 2: Date Prefix
    template2 = NamingTemplate(
        user_id=current_user.id,
        name="Date Prefix",
        template="{{date}} - {{ai_title}}",
        description="Prepends recording date to AI-generated title",
        is_default=False
    )
    templates.append(template2)

    # Default template 3: Date and Time
    template3 = NamingTemplate(
        user_id=current_user.id,
        name="Date and Time",
        template="{{datetime}} {{ai_title}}",
        description="Includes date and time before the AI title",
        is_default=False
    )
    templates.append(template3)

    # Default template 4: Filename Only (no AI)
    template4 = NamingTemplate(
        user_id=current_user.id,
        name="Filename Only",
        template="{{filename}}",
        description="Uses original filename without extension (no AI generation)",
        is_default=False
    )
    templates.append(template4)

    # Default template 5: Phone Call Format
    # For filenames like: 8005551234-5559876543-20260115.wav
    phone_call_patterns = {
        "caller_area": "^(\\d{3})",
        "caller_ex": "^\\d{3}(\\d{3})",
        "caller_sub": "^\\d{6}(\\d{4})",
        "callee_area": "^\\d{10}-(\\d{3})",
        "callee_ex": "^\\d{10}-\\d{3}(\\d{3})",
        "callee_sub": "^\\d{10}-\\d{6}(\\d{4})",
        "year": "^\\d{10}-\\d{10}-(\\d{4})",
        "month": "^\\d{10}-\\d{10}-\\d{4}(\\d{2})",
        "day": "^\\d{10}-\\d{10}-\\d{6}(\\d{2})"
    }
    template5 = NamingTemplate(
        user_id=current_user.id,
        name="Phone Call Format",
        template="{{caller_area}}-{{caller_ex}}-{{caller_sub}} → {{callee_area}}-{{callee_ex}}-{{callee_sub}} ({{year}}-{{month}}-{{day}})",
        description="For phone recordings: caller-callee-YYYYMMDD filenames",
        regex_patterns=json.dumps(phone_call_patterns),
        is_default=False
    )
    templates.append(template5)

    # Add all templates to database
    for template in templates:
        db.session.add(template)

    db.session.commit()

    return jsonify({
        'success': True,
        'templates': [template.to_dict() for template in templates]
    }), 201


@naming_templates_bp.route('/api/naming-templates/<int:template_id>/test', methods=['POST'])
@login_required
def test_naming_template(template_id):
    """Test a naming template with sample data."""
    template = NamingTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    data = request.json or {}
    sample_filename = data.get('filename', 'sample-recording-2026-01-15.mp3')
    sample_date = data.get('date')
    sample_ai_title = data.get('ai_title', 'Meeting with Team')

    # Parse sample date
    meeting_date = None
    if sample_date:
        try:
            meeting_date = datetime.fromisoformat(sample_date)
        except ValueError:
            pass

    if not meeting_date:
        meeting_date = datetime.now()

    # Apply template
    result = template.apply(
        original_filename=sample_filename,
        meeting_date=meeting_date,
        ai_title=sample_ai_title
    )

    return jsonify({
        'result': result or '(empty - would fall back to AI title or filename)',
        'needs_ai_title': template.needs_ai_title(),
        'input': {
            'filename': sample_filename,
            'date': meeting_date.isoformat() if meeting_date else None,
            'ai_title': sample_ai_title
        }
    })


@naming_templates_bp.route('/api/naming-templates/default', methods=['GET'])
@login_required
def get_default_naming_template():
    """Get the user's default naming template."""
    return jsonify({
        'default_naming_template_id': current_user.default_naming_template_id
    })


@naming_templates_bp.route('/api/naming-templates/default', methods=['PUT'])
@login_required
def set_default_naming_template():
    """Set the user's default naming template."""
    data = request.json
    template_id = data.get('template_id') if data else None

    if template_id:
        # Verify template belongs to user
        template = NamingTemplate.query.filter_by(
            id=template_id,
            user_id=current_user.id
        ).first()

        if not template:
            return jsonify({'error': 'Template not found'}), 404

    current_user.default_naming_template_id = template_id if template_id else None
    db.session.commit()

    return jsonify({
        'success': True,
        'default_naming_template_id': current_user.default_naming_template_id
    })
