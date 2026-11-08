"""The registry of UI themes.

A UI theme changes the look of the interface: its neutrals, shapes and type,
and the markup of a few named places (slots). It is chosen per account
(``User.ui_theme``) and combines with the colour scheme and light/dark mode,
which stay separate settings. How a theme is built and added is described in
docs/advanced/ui-themes.md.

Every theme is listed here once. A theme is offered in the picker and accepted
by the preferences endpoint only while ``available`` is true; the others are
registered so their ids, locale keys and folders are reserved, but nobody can
select them until they are ported.
"""

import os
from dataclasses import dataclass

DEFAULT_THEME = 'default'

# The named places in the markup a theme may replace. Each has a default
# partial in templates/themes/default/<slot>.html; a theme folder
# templates/themes/<id>/ holds only the slots it overrides.
SLOTS = (
    'sidebar-nav',             # sidebar header: folder selector, filters, sort and view toggles
    'sidebar-row',             # one row of the sidebar recordings list
    'header-actions',          # the recording's action buttons in the top header (desktop)
    'detail-meta',             # the folder/tag/metadata strip above a recording (desktop)
    'mobile-detail-header',    # the recording header on phones
    'transcript-turn',         # one speaker turn of the simple transcript (desktop)
    'transcript-turn-mobile',  # one speaker turn of the simple transcript (phones)
    'inquire-filters',         # the filter column of the Inquire page
    'status-area',             # the processing queue popup of the main page
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES_DIR = os.path.join(_ROOT, 'templates')
STATIC_DIR = os.path.join(_ROOT, 'static')


@dataclass(frozen=True)
class UiTheme:
    id: str
    available: bool

    @property
    def name_key(self):
        return f'uiTheme.themes.{self.id}.name'

    @property
    def description_key(self):
        return f'uiTheme.themes.{self.id}.description'

    @property
    def preview(self):
        """Path of the preview image under static/."""
        return f'img/themes/{self.id}.webp'

    @property
    def stylesheet(self):
        """Path of the theme's stylesheet under static/, or None without one."""
        path = f'css/themes/{self.id}.css'
        return path if os.path.isfile(os.path.join(STATIC_DIR, path)) else None


UI_THEMES = (
    UiTheme(DEFAULT_THEME, available=True),
    UiTheme('classic', available=True),
    UiTheme('workspace', available=True),
    UiTheme('focus', available=True),
    UiTheme('desk', available=True),
    UiTheme('cards', available=True),
)


def all_themes():
    return UI_THEMES


def get_theme(theme_id):
    for theme in UI_THEMES:
        if theme.id == theme_id:
            return theme
    return None


def available_themes():
    return [theme for theme in UI_THEMES if theme.available]


def is_available(theme_id):
    theme = get_theme(theme_id) if isinstance(theme_id, str) else None
    return bool(theme and theme.available)


def resolve_theme(theme_id):
    """The theme id to apply for a stored value: the value itself while that
    theme is available, else the default (NULL, unknown or withdrawn ids)."""
    return theme_id if is_available(theme_id) else DEFAULT_THEME


def slot_overrides(slot):
    """Ids of the available themes, other than the default, that have their
    own partial for ``slot``, in registry order."""
    if slot not in SLOTS:
        raise ValueError(f'unknown UI theme slot: {slot}')
    return [theme.id for theme in available_themes()
            if theme.id != DEFAULT_THEME
            and os.path.isfile(os.path.join(TEMPLATES_DIR, 'themes', theme.id, f'{slot}.html'))]
