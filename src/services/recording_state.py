"""Per-user archive state (#394).

Archive works like inbox and star: the owner's value lives on the recording,
and each user a recording is shared with keeps their own value on their
SharedRecordingState row, so archiving a shared recording never hides it for
anyone else.
"""

from sqlalchemy import select

from src.database import db
from src.models.sharing import SharedRecordingState


def _state(recording, user):
    return SharedRecordingState.query.filter_by(recording_id=recording.id, user_id=user.id).first()


def get_user_archived(recording, user):
    if recording.user_id == user.id:
        return bool(recording.is_archived)
    state = _state(recording, user)
    return bool(state and state.is_archived)


def set_user_archived(recording, user, value):
    value = bool(value)
    if recording.user_id == user.id:
        recording.is_archived = value
    else:
        state = _state(recording, user)
        if not state:
            state = SharedRecordingState(recording_id=recording.id, user_id=user.id,
                                         is_inbox=True, is_highlighted=False)
            db.session.add(state)
        state.is_archived = value
    db.session.commit()
    return value


def archived_condition(recording_model, user_id):
    """SQL condition: the recording is archived for this user.

    NULL counts as not archived, so rows written before the column existed
    can never disappear from the list.
    """
    shared_archived = select(SharedRecordingState.recording_id).where(
        db.and_(SharedRecordingState.user_id == user_id,
                db.func.coalesce(SharedRecordingState.is_archived, False) == True)  # noqa: E712
    ).scalar_subquery()
    return db.or_(
        db.and_(recording_model.user_id == user_id,
                db.func.coalesce(recording_model.is_archived, False) == True),  # noqa: E712
        recording_model.id.in_(shared_archived),
    )
