"""Named permissions and their resolver (#576).

Each permission has a mode, kept as a System Setting:

    admins    only site admins (the default)
    selected  site admins, users granted the permission, and the members of
              groups granted it
    everyone  every signed-in user

Grants are rows of PermissionGrant. They are kept in every mode, so an
administrator can prepare them and then switch the mode to selected.

A new permission needs one entry in PERMISSIONS and a seeded setting row
(src/init_db.py); user_can() and the grant helpers then work for it as they do
for create_groups.
"""

from src.database import db

MODE_ADMINS = 'admins'
MODE_SELECTED = 'selected'
MODE_EVERYONE = 'everyone'
MODES = (MODE_ADMINS, MODE_SELECTED, MODE_EVERYONE)

CREATE_GROUPS = 'create_groups'

# permission name: (System Setting key of its mode, description of the setting)
PERMISSIONS = {
    CREATE_GROUPS: (
        'permission_create_groups',
        'Who can create groups: admins (site admins only), selected (site admins, '
        'users granted the permission and members of groups granted it) or everyone.',
    ),
}


def _known(permission):
    if permission not in PERMISSIONS:
        raise ValueError(f'unknown permission {permission!r}')


def setting_key(permission):
    """The System Setting key that holds the permission's mode."""
    _known(permission)
    return PERMISSIONS[permission][0]


def permission_for_setting(key):
    """The permission whose mode the setting key holds, or None."""
    for name, (key_, _desc) in PERMISSIONS.items():
        if key_ == key:
            return name
    return None


def permission_mode(permission):
    """The permission's mode; an unknown or missing value counts as admins."""
    from src.models import SystemSetting
    value = SystemSetting.get_setting(setting_key(permission), MODE_ADMINS)
    value = (value or '').strip().lower()
    return value if value in MODES else MODE_ADMINS


def user_has_grant(permission, user_id):
    """Whether the permission is granted to this user individually."""
    from src.models import PermissionGrant
    _known(permission)
    return db.session.query(PermissionGrant.id).filter_by(
        permission=permission, user_id=user_id).first() is not None


def group_has_grant(permission, group_id):
    """Whether the permission is granted to the members of this group."""
    from src.models import PermissionGrant
    _known(permission)
    return db.session.query(PermissionGrant.id).filter_by(
        permission=permission, group_id=group_id).first() is not None


def _granted_through_group(permission, user_id):
    from src.models import GroupMembership, PermissionGrant
    return db.session.query(PermissionGrant.id).join(
        GroupMembership, GroupMembership.group_id == PermissionGrant.group_id
    ).filter(
        PermissionGrant.permission == permission,
        GroupMembership.user_id == user_id,
    ).first() is not None


def user_can(user, permission):
    """Whether the user holds the named permission.

    True for a site admin, for every user when the mode is everyone, and,
    when the mode is selected, for a user granted it individually or a member
    (any role) of a group granted it.
    """
    _known(permission)
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_admin', False):
        return True
    mode = permission_mode(permission)
    if mode == MODE_EVERYONE:
        return True
    if mode != MODE_SELECTED:
        return False
    return user_has_grant(permission, user.id) or _granted_through_group(permission, user.id)


def _set_grant(permission, granted, **target):
    from src.models import PermissionGrant
    _known(permission)
    existing = PermissionGrant.query.filter_by(permission=permission, **target).first()
    if granted and existing is None:
        db.session.add(PermissionGrant(permission=permission, **target))
    elif not granted and existing is not None:
        db.session.delete(existing)


def set_user_grant(permission, user_id, granted):
    """Grant or revoke the permission for one user. Does not commit."""
    _set_grant(permission, granted, user_id=user_id)


def set_group_grant(permission, group_id, granted):
    """Grant or revoke the permission for the members of one group. Does not commit."""
    _set_grant(permission, granted, group_id=group_id)
