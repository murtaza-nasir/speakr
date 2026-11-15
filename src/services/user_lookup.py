"""Finding other accounts: the user_search_scope setting.

One place decides who a signed-in person can find, for the share window
(GET /api/users/search), for the people a share request names
(nameable_user_ids) and for adding a member to a group. The setting has
three levels:

- everyone: part of a username finds any account.
- groups:   part of a username finds only the accounts that share a group
            with the person searching; anyone else needs the full username
            or the full email address.
- exact:    only the full username or the full email address finds an account.

At every level an email address matches only when it is typed in full
(ignoring case), so no answer tells whether part of an address exists, and a
result carries the email address only for a site admin.
"""

import os

from src.database import db
from src.models import GroupMembership, SystemSetting, User
from src.utils.search import LIKE_ESCAPE, contains_pattern
from src.utils.system_settings import env_fallback

SCOPE_EVERYONE = 'everyone'
SCOPE_GROUPS = 'groups'
SCOPE_EXACT = 'exact'
SCOPES = (SCOPE_EVERYONE, SCOPE_GROUPS, SCOPE_EXACT)

SETTING_KEY = 'user_search_scope'
ENV_VAR = 'USER_SEARCH_SCOPE'

MIN_QUERY_LENGTH = 2
MAX_RESULTS = 10


def _valid_scope(value):
    value = value.strip().lower() if isinstance(value, str) else ''
    return value if value in SCOPES else None


def default_search_scope():
    """The scope while no value is saved in System Settings.

    USER_SEARCH_SCOPE from .env when it names a level. Otherwise it follows
    show_usernames_in_ui, as the search did before the setting existed:
    usernames shown means everyone, usernames hidden means exact.
    """
    from_env = _valid_scope(os.environ.get(ENV_VAR))
    if from_env:
        return from_env
    shown = SystemSetting.get_setting('show_usernames_in_ui', env_fallback('SHOW_USERNAMES_IN_UI', False))
    return SCOPE_EVERYONE if shown else SCOPE_EXACT


def search_scope():
    """The level in force: the saved setting, else default_search_scope()."""
    return _valid_scope(SystemSetting.get_setting(SETTING_KEY, None)) or default_search_scope()


def _exact_matches(identifier):
    """Accounts with exactly this username, then the one with exactly this email address."""
    users = User.query.filter(User.username == identifier).all()
    if '@' in identifier:
        by_email = User.query.filter(db.func.lower(User.email) == identifier.lower()).first()
        if by_email is not None and all(user.id != by_email.id for user in users):
            users.append(by_email)
    return users


def find_user_exactly(identifier):
    """The account with exactly this username, or else this email address.

    The email comparison ignores case. Works the same at every level of the
    setting: a person whose username or email is known can always be named.
    """
    identifier = identifier.strip() if isinstance(identifier, str) else ''
    if not identifier:
        return None
    users = _exact_matches(identifier)
    return users[0] if users else None


def search_users(requester, query, scope=None):
    """Up to MAX_RESULTS accounts the requester may find for this query.

    Exact matches come first, then usernames that contain the query in
    username order. The requester is never part of the result, and a query
    under MIN_QUERY_LENGTH characters finds nobody.
    """
    query = query.strip() if isinstance(query, str) else ''
    if len(query) < MIN_QUERY_LENGTH:
        return []
    scope = _valid_scope(scope) or search_scope()

    found = [user for user in _exact_matches(query) if user.id != requester.id][:MAX_RESULTS]
    if scope == SCOPE_EXACT or len(found) >= MAX_RESULTS:
        return found

    partial = User.query.filter(
        User.id != requester.id,
        User.username.ilike(contains_pattern(query), escape=LIKE_ESCAPE),
    )
    if scope == SCOPE_GROUPS:
        own_groups = db.session.query(GroupMembership.group_id).filter(
            GroupMembership.user_id == requester.id)
        partial = partial.filter(User.id.in_(
            db.session.query(GroupMembership.user_id).filter(GroupMembership.group_id.in_(own_groups))))
    if found:
        partial = partial.filter(User.id.notin_([user.id for user in found]))
    return found + partial.order_by(User.username, User.id).limit(MAX_RESULTS - len(found)).all()


def nameable_user_ids(requester, user_ids, named_ids=(), scope=None):
    """The accounts among user_ids that the requester may name as a recipient.

    A numeric id alone must not reach an account the search would not show,
    or a recording owner could run through the ids and read the usernames
    from the list of shares. An id is accepted when the search would find
    its account:

    - everyone: every existing account;
    - groups:   an account that shares a group with the requester;
    - at every level, an account in named_ids: the caller resolved it from
      the full username or the full email address (find_user_exactly), which
      always finds its account.

    A site admin may name every existing account. An unknown id and an id
    that is not accepted are both left out, so the caller answers them the
    same way.
    """
    wanted = {user_id for user_id in user_ids if isinstance(user_id, int) and not isinstance(user_id, bool)}
    if not wanted:
        return set()
    existing = {row[0] for row in db.session.query(User.id).filter(User.id.in_(wanted)).all()}
    if getattr(requester, 'is_admin', False):
        return existing
    scope = _valid_scope(scope) or search_scope()
    if scope == SCOPE_EVERYONE:
        return existing
    allowed = existing & set(named_ids)
    if scope == SCOPE_GROUPS and existing - allowed:
        own_groups = db.session.query(GroupMembership.group_id).filter(
            GroupMembership.user_id == requester.id)
        allowed |= {row[0] for row in db.session.query(GroupMembership.user_id).filter(
            GroupMembership.group_id.in_(own_groups),
            GroupMembership.user_id.in_(existing - allowed)).all()}
    return allowed


def user_result(user, requester):
    """A found account as the API returns it: the email address only for a site admin."""
    result = {'id': user.id, 'username': user.username}
    if getattr(requester, 'is_admin', False):
        result['email'] = user.email
    return result
