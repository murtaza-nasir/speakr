"""The summary prompt one user sees when regenerating a summary (#584).

Everyone sees only their own prompt: the tags they can see on the recording,
the folder prompt when they own the recording, their own default, the admin
default, then the shipped default (resolve_summary_prompt_detail). Nothing
here returns another user's personal default or personal tag prompts.

The Regenerate summary dialog reads describe_summary_prompt() to fill its
prompt and its Start from menu, and calls save_summary_prompt() when the user
saves an edited prompt back to a tag, folder or their own default.

The prompt that produced the current summary is stored with the recording
(Recording.summary_prompt_used). It is returned only to the user whose run
it was: an editor's custom prompt is not shown to the owner, nor the owner's
to an editor.
"""

from sqlalchemy import and_, false, or_

from src.database import db
from src.models import GroupMembership, SystemSetting, Tag
from src.utils.request_body import is_db_id

MAX_PROMPT_LENGTH = 8000


class SummaryPromptError(Exception):
    """A save that cannot be done; code is a server.apiError key."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def _admin_group_ids(user):
    return {m.group_id for m in GroupMembership.query.filter_by(user_id=user.id, role='admin')}


def can_edit_tag(tag, user):
    """The rule of PUT /api/tags/<id>: a personal tag's creator or a group admin."""
    if tag.group_id:
        return tag.group_id in _admin_group_ids(user)
    return tag.user_id == user.id


def can_edit_folder(folder, user):
    """The rule of PUT /api/folders/<id>: a personal folder's owner or a group admin."""
    if folder.group_id:
        return folder.group_id in _admin_group_ids(user)
    return folder.user_id == user.id


def _viewer_tags(user):
    """Tags the user may see: their personal tags and their groups' tags."""
    group_ids = [m.group_id for m in GroupMembership.query.filter_by(user_id=user.id)]
    query = Tag.query.filter(
        or_(and_(Tag.user_id == user.id, Tag.group_id.is_(None)),
            Tag.group_id.in_(group_ids) if group_ids else false()))
    return query.order_by(Tag.name).all()


def _has_prompt(text):
    return bool(text and text.strip())


def _option(kind, prompt, applies, name=None, target_id=None, can_save=False):
    return {
        'key': f'{kind}:{target_id}' if target_id is not None else kind,
        'kind': kind,
        'name': name,
        'id': target_id,
        'prompt': prompt,
        'applies': applies,
        # Where an edit of this prompt can be saved: the tag or folder itself,
        # or, for the defaults, the user's own default. Never the admin default.
        'save_target': ({'kind': kind if kind in ('tag', 'folder') else 'user', 'id': target_id, 'name': name}
                        if can_save else None),
    }


def prompt_source(source, supplier):
    """(kind, name, key) of a prompt resolved by resolve_summary_prompt_detail.

    kind is the source, or 'tags' when several tag prompts were joined; name
    is the tag or folder names; key is the Start from key of the one place
    that holds the prompt ('tags' when no single place does).
    """
    if source == 'tag':
        name = ', '.join(tag.name for tag in supplier)
        if len(supplier) == 1:
            return 'tag', name, f'tag:{supplier[0].id}'
        return 'tags', name, 'tags'
    if source == 'folder':
        return 'folder', supplier.name, f'folder:{supplier.id}'
    return source, None, source


def same_prompt(a, b):
    """Two prompts are the same text, apart from line ends and outer space."""
    def clean(text):
        return (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    return clean(a) == clean(b)


def used_prompt_option(recording, viewer):
    """The Start from entry of the prompt that produced the current summary.

    None unless the viewer's own run produced the summary: the stored text
    can be another user's custom prompt. None also for summaries from before
    the prompt was stored.
    """
    if (not _has_prompt(recording.summary) or not _has_prompt(recording.summary_prompt_used)
            or recording.summary_prompt_user_id != viewer.id):
        return None
    return {
        'key': 'used',
        'kind': 'used',
        'source': recording.summary_prompt_source or 'custom',
        'name': recording.summary_prompt_source_name,
        'id': None,
        'prompt': recording.summary_prompt_used,
        'applies': True,
        # An edit of it can be saved as the viewer's default, like the defaults.
        'save_target': {'kind': 'user', 'id': None, 'name': None},
    }


def describe_summary_prompt(recording, viewer):
    """The viewer's resolved prompt and the prompts they can start from.

    Returns {prompt, source, source_name, source_key, is_owner, save_target,
    start_from, used}. used is the prompt that produced the current summary
    (used_prompt_option), or None. start_from lists first the prompts that apply to this
    recording (its tags with prompts the viewer can see, its folder when the
    viewer owns the recording), then the viewer's default, the admin or
    shipped default and the viewer's other tags with prompts.
    """
    from src.config.prompts import DEFAULT_SUMMARY_PROMPT
    from src.tasks.processing import resolve_summary_prompt_detail

    is_owner = recording.user_id == viewer.id
    prompt, source, supplier = resolve_summary_prompt_detail(recording, viewer)

    options = []
    on_recording = set()
    for tag in recording.get_visible_tags(viewer) or []:
        if _has_prompt(tag.custom_prompt):
            on_recording.add(tag.id)
            options.append(_option('tag', tag.custom_prompt.strip(), True, tag.name, tag.id,
                                   can_save=can_edit_tag(tag, viewer)))
    folder = recording.folder
    if is_owner and folder and _has_prompt(folder.custom_prompt):
        folders_enabled = SystemSetting.get_setting('enable_folders', False)
        options.append(_option('folder', folder.custom_prompt.strip(), True, folder.name, folder.id,
                               can_save=bool(folders_enabled) and can_edit_folder(folder, viewer)))

    if _has_prompt(viewer.summary_prompt):
        options.append(_option('user', viewer.summary_prompt, False, can_save=True))
    admin_default = SystemSetting.get_setting('admin_default_summary_prompt', None)
    if admin_default:
        options.append(_option('admin', admin_default, False, can_save=True))
    else:
        options.append(_option('default', DEFAULT_SUMMARY_PROMPT, False, can_save=True))
    for tag in _viewer_tags(viewer):
        if tag.id not in on_recording and _has_prompt(tag.custom_prompt):
            options.append(_option('tag', tag.custom_prompt.strip(), False, tag.name, tag.id,
                                   can_save=can_edit_tag(tag, viewer)))

    _kind, source_name, source_key = prompt_source(source, supplier)
    resolved = next((o for o in options if o['key'] == source_key), None)

    return {
        'prompt': prompt,
        'source': source,
        'source_name': source_name,
        'source_key': source_key,
        'is_owner': is_owner,
        # Several tag prompts are joined into one, so no single place holds it.
        'save_target': resolved['save_target'] if resolved else None,
        'start_from': options,
        'used': used_prompt_option(recording, viewer),
    }


def save_summary_prompt(recording, viewer, target, target_id, prompt):
    """Save an edited prompt to a tag, the recording's folder or the viewer's default.

    Only targets the viewer may change: a tag they can edit, the folder of a
    recording they own (when they can edit the folder), their own default.
    Raises SummaryPromptError otherwise.
    """
    from src.models import Folder

    text = prompt.strip() if isinstance(prompt, str) else ''
    if not text or len(text) > MAX_PROMPT_LENGTH:
        raise SummaryPromptError('summary_prompt_invalid', 400)

    if target == 'user':
        viewer.summary_prompt = text
    elif target == 'tag':
        tag = db.session.get(Tag, target_id) if is_db_id(target_id) else None
        visible = tag is not None and tag.id in {t.id for t in _viewer_tags(viewer)}
        if not visible or not can_edit_tag(tag, viewer):
            raise SummaryPromptError('summary_prompt_save_denied', 403)
        tag.custom_prompt = text
    elif target == 'folder':
        folder = db.session.get(Folder, target_id) if is_db_id(target_id) else None
        if (folder is None or recording.folder_id != folder.id or recording.user_id != viewer.id
                or not SystemSetting.get_setting('enable_folders', False)
                or not can_edit_folder(folder, viewer)):
            raise SummaryPromptError('summary_prompt_save_denied', 403)
        folder.custom_prompt = text
    else:
        raise SummaryPromptError('summary_prompt_invalid', 400)
    db.session.commit()
