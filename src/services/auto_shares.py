"""Shares that a group tag or a group folder created, kept in step with their reason.

A group tag or group folder shares a recording with the group's members
(source_type 'group_tag' or 'group_folder'). Such a share is valid only while
its reason holds: the recipient is a member of a group that has a tag on the
recording, or of the group whose folder holds the recording. When the reason
goes away (the member leaves, the recording leaves the folder, the tag is
removed or deleted, the group is deleted), the share goes with it (#570,
#582). A share the owner created by hand ('manual') is never touched here.

The recipient's SharedRecordingState is kept, as a manual revoke keeps it,
so personal notes survive; a later share reuses the row (#571).
"""

from src.database import db

AUTO_SOURCES = ('group_tag', 'group_folder')


def ensure_recipient_state(recording_id, user_id):
    """The recipient's SharedRecordingState for a new share, reused if it exists.

    A revoked share leaves the row behind (it holds personal notes), and the
    table allows one row per recording and user, so inserting a second one
    made the commit fail (#571). A reused row returns to the inbox, as a new
    manual share does.
    """
    from src.models import SharedRecordingState
    state = SharedRecordingState.query.filter_by(recording_id=recording_id, user_id=user_id).first()
    if state is None:
        state = SharedRecordingState(recording_id=recording_id, user_id=user_id,
                                     is_inbox=True, is_highlighted=False)
        db.session.add(state)
    else:
        state.is_inbox = True
    return state
