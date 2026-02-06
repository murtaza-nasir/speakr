"""
Export template management API.

This blueprint provides CRUD operations for export templates,
following the same pattern as transcript templates.
"""

import os
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from src.database import db
from src.models import ExportTemplate

# Create blueprint
export_templates_bp = Blueprint('export_templates', __name__)

# Configuration from environment
ENABLE_AUTO_EXPORT = os.environ.get('ENABLE_AUTO_EXPORT', 'false').lower() == 'true'


# --- Routes ---

@export_templates_bp.route('/api/export-templates', methods=['GET'])
@login_required
def get_export_templates():
    """Get all export templates for the current user."""
    templates = ExportTemplate.query.filter_by(user_id=current_user.id).all()
    return jsonify([template.to_dict() for template in templates])


@export_templates_bp.route('/api/export-templates', methods=['POST'])
@login_required
def create_export_template():
    """Create a new export template."""
    data = request.json
    if not data or not data.get('name') or not data.get('template'):
        return jsonify({'error': 'Name and template are required'}), 400

    # If this is set as default, unset other defaults
    if data.get('is_default'):
        ExportTemplate.query.filter_by(
            user_id=current_user.id,
            is_default=True
        ).update({'is_default': False})

    template = ExportTemplate(
        user_id=current_user.id,
        name=data['name'],
        template=data['template'],
        description=data.get('description'),
        is_default=data.get('is_default', False)
    )

    db.session.add(template)
    db.session.commit()

    return jsonify(template.to_dict()), 201


@export_templates_bp.route('/api/export-templates/<int:template_id>', methods=['PUT'])
@login_required
def update_export_template(template_id):
    """Update an existing export template."""
    template = ExportTemplate.query.filter_by(
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
        ExportTemplate.query.filter_by(
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


@export_templates_bp.route('/api/export-templates/<int:template_id>', methods=['DELETE'])
@login_required
def delete_export_template(template_id):
    """Delete an export template."""
    template = ExportTemplate.query.filter_by(
        id=template_id,
        user_id=current_user.id
    ).first()

    if not template:
        return jsonify({'error': 'Template not found'}), 404

    db.session.delete(template)
    db.session.commit()

    return jsonify({'success': True})


@export_templates_bp.route('/api/export-templates/create-defaults', methods=['POST'])
@login_required
def create_default_export_templates():
    """Create default export template for the user if they don't have any."""
    existing_templates = ExportTemplate.query.filter_by(user_id=current_user.id).count()

    if existing_templates > 0:
        return jsonify({'message': 'User already has templates'}), 200

    # Default template with localized labels
    default_template = ExportTemplate(
        user_id=current_user.id,
        name="Standard Export",
        template="""# {{title}}

## {{label.metadata}}

{{#if meeting_date}}- **{{label.date}}:** {{meeting_date}}
{{/if}}{{#if created_at}}- **{{label.created}}:** {{created_at}}
{{/if}}{{#if original_filename}}- **{{label.originalFile}}:** {{original_filename}}
{{/if}}{{#if file_size}}- **{{label.fileSize}}:** {{file_size}}
{{/if}}{{#if participants}}- **{{label.participants}}:** {{participants}}
{{/if}}{{#if tags}}- **{{label.tags}}:** {{tags}}
{{/if}}

{{#if notes}}## {{label.notes}}

{{notes}}

{{/if}}{{#if summary}}## {{label.summary}}

{{summary}}

{{/if}}{{#if transcription}}## {{label.transcription}}

{{transcription}}

{{/if}}""",
        description="Default export template with localized labels",
        is_default=True
    )

    db.session.add(default_template)
    db.session.commit()

    return jsonify({
        'success': True,
        'templates': [default_template.to_dict()]
    }), 201
