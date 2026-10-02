"""Helpers for recording titles at upload/share time.

These are the single source of truth for two related decisions that were
previously duplicated inline across the upload route, the PWA share-target
route, and the AI-title-generation task:

1. What title a freshly-created recording should get (`resolve_upload_title`).
2. Whether a title is an auto-generated placeholder that the AI title
   generator is allowed to overwrite (`is_placeholder_title`).

Keeping these together guarantees every entry point (drag-drop upload, share
target, black-hole auto-import) produces a title that the title task
recognises, so shared/auto-imported files get an AI title exactly like a
normal upload instead of being silently skipped.
"""


def placeholder_title(original_filename):
    """The default placeholder title for an upload with no user-supplied title."""
    return f"Recording - {original_filename}"


def placeholder_titles(original_filename):
    """All placeholder titles the AI title generator may overwrite."""
    return (
        f"Recording - {original_filename}",
        f"Auto-processed - {original_filename}",
    )


def resolve_upload_title(user_title, original_filename):
    """The title a newly-uploaded or shared recording should be created with.

    A non-empty user-supplied title is used as-is (and the AI title task will
    leave it alone). Otherwise we return a placeholder the title task
    recognises, so AI title generation runs.
    """
    if user_title and user_title.strip():
        return user_title.strip()
    return placeholder_title(original_filename)


def upload_title_source(user_title):
    """title_source for a recording created with this form value: 'user' when a
    title was typed, else None (a placeholder the title step replaces)."""
    return 'user' if user_title and user_title.strip() else None


def title_is_user_chosen(recording):
    """True when the title step must keep the recording's title (#412).

    'user' titles are kept and 'auto' titles are regenerated, for example on a
    transcription reprocess. Recordings from before title_source existed keep
    the earlier rule: a placeholder gets a title, anything else is kept.
    """
    source = getattr(recording, 'title_source', None)
    if source == 'user':
        return True
    if source == 'auto':
        return False
    return not is_placeholder_title(recording.title, recording.original_filename)


def is_placeholder_title(title, original_filename):
    """True if `title` is empty or an auto-generated placeholder.

    The AI title generator overwrites placeholders; a non-placeholder title is
    treated as user-chosen and left untouched.
    """
    if not title:
        return True
    return title in placeholder_titles(original_filename)
