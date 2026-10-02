"""The title step of the processing pipeline.

Every path that gives a recording its generated title calls these functions:
the background title task after transcription and the regenerate-title
endpoint (web and API v1). The title is therefore the same whichever path
produced it, and a rule added here reaches all of them (#412).

Order of the title rules:
1. Naming template: the first tag on the recording with a template, then the
   recording's folder, then the owner's default template.
2. A template without {{ai_title}} needs no LLM call.
3. Otherwise the AI title is generated and passed through the template.
4. Fallback: template result, then AI title, then the filename without its
   extension.
"""

import os

from flask import current_app

from src.database import db
from src.models import Folder


def resolve_naming_template(recording):
    """The naming template for a recording: tag, then folder, then the owner's default.

    Returns (template, source) where source is 'tag', 'folder', 'user' or None.
    """
    for tag in recording.tags:
        if tag.naming_template_id and tag.naming_template:
            return tag.naming_template, 'tag'
    if recording.folder_id:
        folder = db.session.get(Folder, recording.folder_id)
        if folder and folder.naming_template_id and folder.naming_template:
            return folder.naming_template, 'folder'
    owner = recording.owner
    if owner and owner.default_naming_template_id and owner.default_naming_template:
        return owner.default_naming_template, 'user'
    return None, None


def needs_ai_title(recording):
    """True when the title for this recording requires an LLM call."""
    template, _ = resolve_naming_template(recording)
    return template is None or template.needs_ai_title()


def compute_title(recording, *, raise_budget_errors=False):
    """The title the pipeline gives this recording now, without saving it.

    TokenBudgetExceeded propagates when raise_budget_errors is True (the
    interactive endpoint shows the reason); background callers skip the AI
    title instead.
    """
    # Read through the processing module so tests and callers that patch
    # processing.client / processing._generate_ai_title affect this step too.
    from src.tasks import processing
    from src.services.llm import TokenBudgetExceeded

    template, source = resolve_naming_template(recording)
    if template:
        current_app.logger.info(
            f"Using naming template '{template.name}' ({source}) for recording {recording.id}")

    ai_title = None
    if template is not None and not template.needs_ai_title():
        current_app.logger.info(f"Naming template needs no AI title for recording {recording.id}")
    elif processing.client is None:
        current_app.logger.warning(f"Skipping AI title for {recording.id}: LLM client not configured.")
    elif not recording.transcription or len(recording.transcription.strip()) < 10:
        current_app.logger.warning(f"Transcription for recording {recording.id} is too short for an AI title.")
    else:
        try:
            ai_title = processing._generate_ai_title(recording)
        except TokenBudgetExceeded as e:
            if raise_budget_errors:
                raise
            current_app.logger.warning(f"Skipping AI title for recording {recording.id}: {e}")

    title = None
    if template:
        title = template.apply(
            original_filename=recording.original_filename,
            meeting_date=recording.meeting_date,
            ai_title=ai_title,
        )
    if not title:
        title = ai_title
    if not title and recording.original_filename:
        title = os.path.splitext(recording.original_filename)[0]
        current_app.logger.info(f"Using filename as title for recording {recording.id}: '{title}'")
    return title
