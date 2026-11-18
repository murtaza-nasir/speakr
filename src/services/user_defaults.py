"""Defaults for user settings (#584).

An administrator sets a default for each user setting listed in DEFAULTS, in
System Settings (the admin page shows them under "User defaults"). Each
default is a System Setting; most fall back to a variable in .env and then to
a built-in value.

New accounts: every default is written on the account with
apply_new_user_defaults(user), wherever an account is created (registration,
admin, SSO, the admin scripts) and never for an account that already exists.

Existing accounts: when a default is changed, the administrator may apply the
change to the accounts that still have the previous default. They are counted
with accounts_on() and updated in one statement with apply_to_existing(). An account
with any other value was changed by its user and is left alone. NULL in a
column counts as the previous default only where NULL means that value (see
null_value of each default).

A setting that was changed on an account is listed in User.chosen_settings
from then on (mark_changed_settings, called before every update of a user
row). Such an account is left alone for that setting, also after its user
went back to the value of the default. Accounts from before the column have
no list: for them only the value tells.

Every row of the user table is an account of a person; Speakr has no service
accounts, so all rows are counted.
"""

import logging
import os

from sqlalchemy import and_, event, false, inspect, not_, or_

from src.config import ui_themes

logger = logging.getLogger(__name__)

AUTO = 'auto'
_TRUE_WORDS = ('true', '1', 'yes')
_FALSE_WORDS = ('false', '0', 'no')


class UserDefault:
    """One default: a System Setting and the user column it is written to.

    A value is always the text stored in the setting ('emerald', 'auto',
    'true'); to_column() gives the value of the user column for it.
    """

    key = None
    env = None                  # .env variable, or None
    builtin = None              # value without a setting and without .env
    column = None               # attribute of User
    setting_type = 'string'
    null_value = None           # the value that NULL in the column means
    error_code = 'invalid_user_default'

    def choices(self):
        """The accepted values in display order, or None for an open list."""
        return None

    def clean(self, raw):
        """The value as it is stored, or ValueError."""
        value = str(raw).strip().lower() if raw is not None else ''
        if value not in self.choices():
            raise ValueError(value)
        return value

    def options(self):
        """Values with what the admin page needs to label them, or None."""
        return None

    def error_params(self):
        return {'setting': self.key, 'allowed': ', '.join(self.choices() or ())}

    def fallback(self):
        """The value without a saved setting: .env, else the built-in value."""
        raw = os.environ.get(self.env) if self.env else None
        if raw is not None and raw.strip():
            try:
                return self.clean(raw)
            except ValueError:
                ui_themes._warn_once(self.env, raw, self.builtin)
        return self.builtin

    def current(self):
        """The default in effect."""
        from src.models import SystemSetting
        saved = SystemSetting.get_setting(self.key, None)
        if saved is not None and str(saved).strip():
            try:
                return self.clean(saved)
            except ValueError:
                pass
        return self.fallback()

    def to_column(self, value):
        return value

    def _column(self):
        from src.models import User
        return getattr(User, self.column)

    def condition(self, value):
        """The accounts whose setting equals ``value``."""
        column, stored = self._column(), self.to_column(value)
        if stored is None:
            return or_(column.is_(None), column == '')
        condition = column == stored
        if value == self.null_value:
            condition = or_(condition, column.is_(None))
        return condition

    def assignment(self, value):
        """Column values that give an account ``value``."""
        return {self.column: self.to_column(value)}


class _Choice(UserDefault):
    def __init__(self, key, env, builtin, column, choices, null_value=None, error_code=None):
        self.key, self.env, self.builtin, self.column = key, env, builtin, column
        self._choices, self.null_value = choices, null_value
        if error_code:
            self.error_code = error_code

    def choices(self):
        return list(self._choices() if callable(self._choices) else self._choices)


class _Look(_Choice):
    """UI theme, color scheme and audio player position. Their values are
    resolved in src/config/ui_themes.py, which the signed-out pages use too."""

    def __init__(self, *args, resolve, invalid, params, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolve, self._invalid, self._params = resolve, invalid, params

    def error_params(self):
        return {self._params: ', '.join(self.choices())}

    def current(self):
        return self._resolve()

    def fallback(self):
        from src.utils.system_settings import env_fallback
        value = str(env_fallback(self.env, self.builtin)).strip().lower()
        return value if value in self.choices() else self._invalid


class _Flag(UserDefault):
    setting_type = 'boolean'

    def __init__(self, key, env, builtin, column, null_value):
        self.key, self.env, self.builtin, self.column = key, env, builtin, column
        self.null_value = null_value

    def choices(self):
        return ['true', 'false']

    def clean(self, raw):
        if isinstance(raw, bool):
            return 'true' if raw else 'false'
        value = str(raw).strip().lower() if raw is not None else ''
        if value in _TRUE_WORDS:
            return 'true'
        if value in _FALSE_WORDS:
            return 'false'
        raise ValueError(value)

    def to_column(self, value):
        return value == 'true'

    def condition(self, value):
        column = self._column()
        condition = column.is_(value == 'true')
        if value == self.null_value:
            condition = or_(condition, column.is_(None))
        return condition


class _TranscriptionLanguage(UserDefault):
    """'auto' (the service detects the language, NULL on the account) or a
    code of the shared ASR list."""

    key, env, builtin = 'default_transcription_language', 'DEFAULT_TRANSCRIPTION_LANGUAGE', AUTO
    column = 'transcription_language'

    def clean(self, raw):
        from src.utils.language import SUPPORTED_CODES
        value = str(raw).strip().lower() if raw is not None else ''
        if value == AUTO or value in SUPPORTED_CODES:
            return value
        raise ValueError(value)

    def error_params(self):
        return {'setting': self.key, 'allowed': 'auto, en, de, es, ...'}

    def to_column(self, value):
        return None if value == AUTO else value


class _OutputLanguage(UserDefault):
    """'auto' (the model decides, NULL on the account) or a language name of
    OUTPUT_LANGUAGES."""

    key, env, builtin = 'default_output_language', 'DEFAULT_OUTPUT_LANGUAGE', AUTO
    column = 'output_language'

    def choices(self):
        from src.utils.language import OUTPUT_LANGUAGES
        return [AUTO] + [name for name, _code in OUTPUT_LANGUAGES]

    def clean(self, raw):
        from src.utils.language import canonical_translation_language
        value = str(raw).strip() if raw is not None else ''
        if value.lower() == AUTO:
            return AUTO
        name = canonical_translation_language(value)
        if not name:
            raise ValueError(value)
        return name

    def options(self):
        from src.utils.language import OUTPUT_LANGUAGES
        return [{'value': name, 'code': code} for name, code in OUTPUT_LANGUAGES]

    def error_params(self):
        return {'setting': self.key, 'allowed': 'auto, English, German, Spanish, ...'}

    def to_column(self, value):
        return None if value == AUTO else value


class _InterfaceLanguage(_Choice):
    def options(self):
        from src.utils.localization import UI_LANGUAGES
        return [{'value': code, 'label': label} for code, label in UI_LANGUAGES]

    def clean(self, raw):
        from src.utils.localization import match_language
        value = match_language(str(raw)) if raw is not None else None
        if not value:
            raise ValueError(raw)
        return value


class _TimezoneMode(_Choice):
    """'auto': the zone of the user's browser is saved as the user visits.
    'fixed': the account has the default time zone (default_timezone) as a
    fixed zone, as if chosen in Account settings."""

    def condition(self, value):
        from src.models import User
        from src.utils.timezones import MODE_AUTO, MODE_FIXED, deployment_timezone
        if value == MODE_FIXED:
            return and_(User.timezone_mode == MODE_FIXED, User.timezone == deployment_timezone())
        return or_(User.timezone_mode.is_(None), User.timezone_mode.in_((MODE_AUTO, '')))

    def assignment(self, value):
        from src.utils.timezones import MODE_AUTO, MODE_FIXED, deployment_timezone
        if value == MODE_FIXED:
            return {'timezone_mode': MODE_FIXED, 'timezone': deployment_timezone()}
        # The zone already saved stays until the browser reports its own.
        return {'timezone_mode': MODE_AUTO}


class _Timezone(UserDefault):
    """The default time zone (default_timezone). It is read live for accounts
    without a zone, so it is written on accounts only while the default mode
    is 'fixed', and only those accounts can still be on a previous value."""

    key, builtin, column = 'default_timezone', 'UTC', 'timezone'
    error_code = 'invalid_timezone_value'

    def clean(self, raw):
        from src.utils.timezones import is_valid_timezone
        value = str(raw).strip() if raw is not None else ''
        if not is_valid_timezone(value):
            raise ValueError(value)
        return value

    def error_params(self):
        return {}

    def current(self):
        from src.utils.timezones import deployment_timezone
        return deployment_timezone()

    def condition(self, value):
        from src.models import User
        from src.utils.timezones import MODE_FIXED
        if get('default_timezone_mode').current() != MODE_FIXED:
            return false()
        return and_(User.timezone_mode == MODE_FIXED, User.timezone == value)

    def assignment(self, value):
        # New accounts get the zone through default_timezone_mode.
        return {'timezone': value}


def _interface_languages():
    from src.utils.localization import UI_LANGUAGES
    return [code for code, _label in UI_LANGUAGES]


DEFAULTS = (
    _Look('default_ui_theme', 'DEFAULT_UI_THEME', ui_themes.NEW_USER_THEME, 'ui_theme',
          lambda: [theme.id for theme in ui_themes.available_themes()],
          null_value=ui_themes.DEFAULT_THEME, error_code='invalid_default_ui_theme',
          resolve=ui_themes.new_user_theme, invalid=ui_themes.DEFAULT_THEME, params='themes'),
    _Look('default_color_scheme', 'DEFAULT_COLOR_SCHEME', ui_themes.NEW_USER_COLOR_SCHEME, 'theme_color_scheme',
          ui_themes.COLOR_SCHEMES,
          null_value=ui_themes.DEFAULT_COLOR_SCHEME, error_code='invalid_default_color_scheme',
          resolve=ui_themes.new_user_color_scheme, invalid=ui_themes.DEFAULT_COLOR_SCHEME, params='schemes'),
    _Look('default_audio_player_position', 'DEFAULT_AUDIO_PLAYER_POSITION',
          ui_themes.NEW_USER_AUDIO_PLAYER_POSITION, 'audio_player_position',
          ui_themes.AUDIO_PLAYER_POSITIONS,
          null_value=ui_themes.DEFAULT_AUDIO_PLAYER_POSITION, error_code='invalid_default_audio_player_position',
          resolve=ui_themes.new_user_audio_player_position, invalid=ui_themes.DEFAULT_AUDIO_PLAYER_POSITION,
          params='positions'),
    _InterfaceLanguage('default_ui_language', 'DEFAULT_UI_LANGUAGE', 'en', 'ui_language',
                       _interface_languages, null_value='en'),
    _TranscriptionLanguage(),
    _OutputLanguage(),
    _TimezoneMode('default_timezone_mode', 'DEFAULT_TIMEZONE_MODE', AUTO, 'timezone_mode', (AUTO, 'fixed')),
    _Timezone(),
    _Flag('default_auto_summarization', 'DEFAULT_AUTO_SUMMARIZATION', 'true', 'auto_summarization', 'true'),
    _Flag('default_auto_speaker_labelling', 'DEFAULT_AUTO_SPEAKER_LABELLING', 'false',
          'auto_speaker_labelling', 'false'),
    _Flag('default_extract_events', 'DEFAULT_EXTRACT_EVENTS', 'false', 'extract_events', 'false'),
)
_BY_KEY = {default.key: default for default in DEFAULTS}
KEYS = tuple(_BY_KEY)


def get(key):
    """The default of a setting key, or None for any other key."""
    return _BY_KEY.get(key) if isinstance(key, str) else None


def apply_new_user_defaults(user):
    """Give a user who is being created the defaults for new accounts. Called
    wherever an account is created (registration, admin, SSO, the admin
    scripts); never for an account that already exists."""
    for default in DEFAULTS:
        if default.key == 'default_timezone':
            continue  # written through default_timezone_mode
        for column, value in default.assignment(default.current()).items():
            setattr(user, column, value)
    return user


def _marker(key):
    return f',{key},'


def _unchosen(default):
    """The accounts on which the setting was never changed."""
    from src.models import User
    return or_(User.chosen_settings.is_(None),
               not_(User.chosen_settings.contains(_marker(default.key), autoescape=True)))


def _changed(state, column):
    history = state.attrs[column].history
    if not history.has_changes() or not history.added:
        return False
    old = history.deleted[0] if history.deleted else None
    return (old or None) != (history.added[0] or None)


def mark_changed_settings(user):
    """List in chosen_settings the settings whose value is being changed on
    an account that exists. The zone that a browser reports in automatic mode
    is not a choice of the default time zone."""
    from src.utils.timezones import MODE_FIXED
    state = inspect(user)
    chosen = user.chosen_settings or ','
    for default in DEFAULTS:
        if _marker(default.key) in chosen or not _changed(state, default.column):
            continue
        if default.key == 'default_timezone' and user.timezone_mode != MODE_FIXED:
            continue
        chosen += default.key + ','
    if chosen != (user.chosen_settings or ','):
        user.chosen_settings = chosen


def _register_listener():
    from src.models import User

    @event.listens_for(User, 'before_update')
    def _before_update(_mapper, _connection, target):
        mark_changed_settings(target)


def accounts_on(default, value):
    """The number of accounts that have ``value`` and never changed the setting."""
    from src.models import User
    return User.query.filter(and_(default.condition(value), _unchosen(default))).count()


def apply_to_existing(default, old, new):
    """Give the accounts that still have ``old`` the value ``new``, in one
    statement. Returns the number of accounts updated. The caller commits."""
    from src.models import User
    if old == new:
        return 0
    return User.query.filter(and_(default.condition(old), _unchosen(default))).update(
        default.assignment(new), synchronize_session=False)


def describe():
    """The defaults for the admin page: key, value in effect, type and the
    accepted values (None where the page has the list itself)."""
    from src.models import SystemSetting
    rows = {row.key: row for row in SystemSetting.query.filter(SystemSetting.key.in_(KEYS)).all()}
    result = []
    for default in DEFAULTS:
        row = rows.get(default.key)
        result.append({
            'key': default.key,
            'value': default.current(),
            'setting_type': default.setting_type,
            'choices': default.choices(),
            'options': default.options(),
            'source': 'env' if default.env and (row is None or row.value is None) else 'setting',
            'description': row.description if row is not None else None,
        })
    return result


_register_listener()
