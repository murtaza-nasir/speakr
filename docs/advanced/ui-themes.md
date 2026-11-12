# UI Themes

A UI theme is a complete look for the interface: neutral surfaces, shapes, type and density, plus its own markup in a few named places. Each account picks one theme. The theme is independent of the six color schemes and of light or dark mode, which keep working with every theme. This page describes how themes are built and how a new one is added.

## Overview

| Piece | Where | Purpose |
|---|---|---|
| Registry | `src/config/ui_themes.py` | Theme ids, availability, slot names, locale keys, preview and stylesheet paths |
| Setting | `User.ui_theme` (`VARCHAR(32)`, null = default) | The account's theme; the column is added at startup in `src/init_db.py` |
| Save endpoint | `POST /api/user/preferences` with `ui_theme` | An available theme id or `null` is saved; anything else is answered with 400 `invalid_ui_theme` |
| Account copy | `templates/includes/theme_account.html` | `window.speakrThemeDefaults` for a browser with nothing saved, and a copy of the account's theme in `localStorage.uiTheme`, made before the theme is read; changes are saved with `window.speakrSaveTheme('uiTheme')` |
| New accounts | `new_user_theme()`, `new_user_color_scheme()`, `apply_new_user_theme(user)` in `src/config/ui_themes.py` | The look written on every account when it is created, from the System Settings `default_ui_theme` and `default_color_scheme` (`.env`: `DEFAULT_UI_THEME`, `DEFAULT_COLOR_SCHEME`; defaults `cards` and `emerald`) |
| Pre-paint script | `templates/includes/ui_theme.html` | `<html data-ui-theme="<id>">` and the theme stylesheet, set before the first paint; `window.speakrApplyUiTheme(id)` for switching |
| Theme stylesheet | `static/css/themes/<id>.css` | The theme's CSS, scoped under `html[data-ui-theme="<id>"]`; none for the default |
| Slots | `templates/themes/slot.html`, `templates/themes/<id>/<slot>.html` | Markup a theme replaces |
| Reactive value | `uiTheme` in the main app (`static/js/modules/composables/ui-theme.js`) and in the Inquire app (`templates/inquire.html`) | The value the slot branches switch on, without a reload |
| Picker | `templates/includes/ui_theme_picker.html` | Theme cards in Account > Preferences > Appearance and in the color scheme dialogs |
| Previews | `static/img/themes/<id>.webp` | Card images |
| Guard test | `tests/test_ui_theme_slots.py` | Check that overrides keep every behaviour of the default markup |

The registered ids are `default` (the standard look), `classic`, `workspace`, `focus`, `desk` and `cards`. Only `default` is available. A theme that is registered but not available has reserved locale keys and a reserved folder name, and it is neither offered in the picker nor accepted by the endpoint.

The picker appears only while two or more themes are available, so with the default alone no page shows it.

## How a theme is applied

1. In `includes/theme_account.html` (first in `<head>` on every page), `window.speakrThemeDefaults` is set to the color scheme and theme used when `localStorage` has none: the defaults for new accounts on a signed-out page, and `blue` and `default` for a signed-in account. For a signed-in account, the account's color scheme and theme are then written into `localStorage.colorScheme` and `localStorage.uiTheme`, with `blue` and `default` for an account with no saved value. The look of a signed-out page therefore never stays after sign-in. Light or dark mode is copied only once the account has a saved value.
2. `includes/ui_theme.html` is included right after the compiled `tailwind.css` link on every page with a `<head>` (the main app, Inquire, account, admin, group management, shared recordings and the sign-in pages). In its script, `localStorage.uiTheme` is read, an id that is not in the list of available themes is replaced by `default`, `data-ui-theme` is set on `<html>`, and for a theme with a stylesheet `<link id="speakr-ui-theme-css" rel="stylesheet" blocking="render">` is appended to `<head>`, after every other stylesheet. All of this happens before the body is parsed, so the first paint already has the theme.
3. In each slot, the active theme's partial is rendered through a `v-if` chain on the reactive `uiTheme`.

Switching is done with `window.speakrApplyUiTheme(id)`. With that call, the attribute is updated, the stylesheet link is replaced or removed, the `aria-pressed` state of the theme cards outside Vue is updated, `localStorage.uiTheme` is written, the choice is saved to the account, and the window event `speakr:ui-theme` is dispatched with `detail.id`. On that event `uiTheme` is updated in the Vue apps, so the slot branches switch without a reload. Other tabs of the same browser are switched through the `storage` event, without saving again.

Signed-out pages (sign-in, registration, password reset, public share pages) have no account value. On them, the theme and color scheme saved in that browser are shown, or, with nothing saved, the defaults for new accounts. Nothing is written to `localStorage` on a signed-out page until the visitor changes the theme.

`apply_new_user_theme(user)` is called wherever an account is created: registration (`src/api/auth.py`), `POST /admin/users` (`src/api/admin.py`), the first SSO sign-in (`src/auth/sso.py`), and `scripts/create_admin.py` and `scripts/docker_create_admin.py`. In `tests/test_new_user_theme.py`, every file in `src/` and `scripts/` that constructs a `User` is checked for the call. Accounts are never updated afterwards, so a NULL `ui_theme` or `theme_color_scheme` still means `default` or `blue`.

## Colors: the contract between themes and color schemes

The color scheme and light/dark mode are classes on `<html>`: `dark`, and `theme-light-<scheme>` or `theme-dark-<scheme>` for every scheme except blue (`static/css/styles.css`). A theme's rules are scoped under `html[data-ui-theme="<id>"]`, which is more specific than those classes, so a variable set by the theme on `html` wins over the scheme's value. That is why the variables are split in two groups.

**From the color scheme (a theme must not set these):**

| Variable | Use |
|---|---|
| `--bg-accent`, `--bg-accent-hover` | Tinted accent surfaces: the selected row, active filters |
| `--bg-button`, `--bg-button-hover` | Filled primary controls |
| `--text-accent` | Accent text and icons |
| `--border-accent` | Accent borders |
| `--border-focus`, `--ring-focus` | Focus outline and ring |

Any other accent shade is derived from these, for example `color-mix(in srgb, var(--bg-button) 12%, transparent)`, so it follows every scheme.

**From the theme (a theme must define these for light and for dark):**

| Variable | Use |
|---|---|
| `--bg-primary`, `--bg-secondary`, `--bg-tertiary`, `--bg-input`, `--bg-audio-player` | Page, panels, insets, inputs |
| `--text-primary`, `--text-secondary`, `--text-muted`, `--text-light` | Text levels |
| `--border-primary`, `--border-secondary` | Dividers and control borders |
| `--scrollbar-track`, `--scrollbar-thumb`, `--scrollbar-thumb-hover` | Scrollbars |

Light values go under `html[data-ui-theme="<id>"]`, dark values under `html[data-ui-theme="<id>"].dark`. The schemes also tint the neutrals slightly. When a theme sets the neutral variables, that tint is replaced by the theme's neutrals, and the accent still follows the scheme.

**Optional for a theme:** the shape and type tokens of `static/css/primitives.css` (`--radius-*`, `--shadow-*`, `--sp-*`, `--fs-*`, `--lh-*`) and the status colors (`--bg-danger*`, `--text-danger*`, `--tint-*` and the other semantic colors). The status colors keep their meaning in every theme; adjust their tone for contrast only.

A theme can define its own tokens on top of these (for example `--surface` or `--radius-card`), as long as they are declared inside its scope.

## Slots

A slot is a named place in the markup that a theme can replace. The default markup of each slot lives in `templates/themes/default/<slot>.html` and was moved there unchanged from the templates below. The place in the page is a line such as:

```
{% with ui_slot = 'sidebar-row' %}{% include 'themes/slot.html' %}{% endwith %}
```

With no available theme overriding a slot, the output of `themes/slot.html` is the default partial alone, so the page is identical to the page without slots. When available themes override it, the output is:

```html
<template v-if="uiTheme === 'classic'">...templates/themes/classic/sidebar-row.html...</template>
<template v-else-if="uiTheme === 'cards'">...templates/themes/cards/sidebar-row.html...</template>
<template v-else>...templates/themes/default/sidebar-row.html...</template>
```

Only one branch is rendered at a time, so an id or a `ref` that appears in two branches does not clash. A theme folder holds only the slots it overrides; every other slot uses the default partial. An override may include the default partial (`{% include 'themes/default/<slot>.html' %}`) and add markup around it.

| Slot | Template it came from | Why it is a slot |
|---|---|---|
| `sidebar-nav` | `components/sidebar.html`, sidebar header | The folder selector, New and Select buttons, filter panel, sort and view toggles. This area was rebuilt in every design (a navigation rail, a tree with counts, a column header, pill toggles). |
| `sidebar-row` | `components/sidebar.html`, row of the recordings list | One recording in the list. Its structure changed in four designs: a time column, a single-line row, a table row with columns, a card with an avatar footer. |
| `header-actions` | `components/header.html` | The recording's action buttons in the top header on desktop. In one design most of them were moved into an overflow menu, in another into a toolbar above the recording. |
| `detail-meta` | `components/detail-view.html` | The folder, tag and metadata strip above a recording on desktop. The chips carry their color in inline styles, which were replaced in three designs; in one design the recording toolbar is added here. |
| `mobile-detail-header` | `components/detail/mobile-header.html` (moved whole) | The recording header on phones: same chip styles, plus layout hooks and a separate row of tab actions in one design. |
| `transcript-turn` | `components/detail/desktop-transcription-panel.html` | One speaker turn of the simple transcript on desktop. A timestamp and speaker column or an avatar is added in two designs. |
| `transcript-turn-mobile` | `components/detail/mobile-transcript-panel.html` | The same on phones. |
| `inquire-filters` | `inquire.html`, the filter column | The Inquire page's left column. Its groups were restyled in every design (disclosures, compact rows with counts, chips, the sidebar's navigation style). |
| `status-area` | `components/progress-popup.html` (moved whole) | The processing queue popup of the main page. In one design a status bar is added beside it, with a button for the queue. |

Changes that only add a class or adjust an inline style on shared chrome (the header bar, the account tab strip, the sign-in body) are made in the theme's CSS against the existing selectors instead of in a slot. A JavaScript default that differs per theme (for example whether the queue popup starts minimized) is read from `uiTheme` in `static/js/app.modular.js`. A formatting helper needed by a theme partial is added to the main app's returned scope, where it is available to every theme.

### Shared hooks a theme can use

Two pieces of shared markup are prepared for themes and need no slot.

| Hook | Where | How a theme uses it |
|---|---|---|
| Back link | `<a class="page-header__back">` in `templates/components/page-header.html`, the header of the Account, Admin and group management pages. It holds an arrow icon and the label `nav.backToRecordings`, and links to the recordings. | It is `display: none` in `static/css/primitives.css`, so no theme shows it by default. A theme shows it with `html[data-ui-theme="<id>"] .page-header__back { display: inline-flex; }` and styles it like the other header buttons of the theme (workspace and desk do this). |
| Close on outside click | `<details data-close-outside>` | `static/js/modules/utils/close-outside.js`, installed in the main app and in the header app, closes every open menu of this kind when a click lands outside it and when Escape is pressed. A theme menu built on `<details>` (for example the overflow menu of the Focus header) adds the attribute and keeps any handlers it already has. |

The floating chat button (`.floating-chat-fab`) is 44px and see-through (`opacity: 0.6`) until it is hovered or focused. A theme that overrides it keeps a target of at least 44px and does not set `opacity`; a `transition` override includes `opacity` so the change is smooth.

## The guard test

In `tests/test_ui_theme_slots.py`, every override, `templates/themes/<id>/<slot>.html`, is compared with the default partial of the same slot. An override may add behaviour and change markup and classes freely, but it must keep everything in this list that the default has:

| Kind | What is compared |
|---|---|
| Handlers | `@event` and `v-on:event` attributes, by their handler expression (whitespace-normalized; the event name and modifiers may change) |
| `v-model` | The bound target, with any modifiers |
| Refs | `ref` and `:ref` values |
| Ids | `id` and `:id` values, used to look elements up in scripts and tests |
| Data attributes | The names of all `data-*` attributes, static or bound (for example `data-header-more-menu`, used by the outside-click handler, and `:data-start-time`, read by the player); for `data-i18n*` attributes the translation key counts as well |
| Accessibility labels | The translation keys in `t('...')` inside `aria-label`, `title`, `alt` and `placeholder`, static or bound |

A justified difference goes into `ALLOWED` in the test, keyed by `(theme, slot)` and kind, with a comment that gives the reason. When a theme moves markup from one slot to another, `MOVED` lists the slots whose overrides together must keep what the default slot had; for example, a theme that moves the header actions into a toolbar in `detail-meta` declares `('<id>', 'header-actions'): ('detail-meta',)`.

Further checks in the same file: every slot has a default partial and is placed exactly once, theme folders hold only registered themes and slots, every registered theme has a name and description in all seven locales, every available theme has a WebP preview, and every rule of `static/css/themes/<id>.css` is scoped under `html[data-ui-theme="<id>"]`. The setting, the endpoint, the pre-paint markup, the picker and the slot branches are tested in `tests/test_ui_themes.py`, and the pre-paint script is run against a stub browser in `static/js/modules/composables/ui-theme.test.js`.

## Adding a theme

1. **Registry.** The theme is already listed in `UI_THEMES` in `src/config/ui_themes.py` if it is one of the five designs; otherwise add a `UiTheme('<id>', available=False)` entry. Keep `available=False` until the steps below are done.
2. **Locale keys.** Add `uiTheme.themes.<id>.name` and `.description` (one line) to all seven files in `static/locales/`, with real translations.
3. **Stylesheet.** Create `static/css/themes/<id>.css`. Start every selector with `html[data-ui-theme="<id>"]`, including the ones inside `@media`. Define the neutral variables for light and dark (see the color contract) and leave the accent variables to the scheme. The file is served as it is; no build step is needed. The stylesheet is loaded after `tailwind.css`, and with the scope each rule is more specific than a single Tailwind utility, so `!important` is rarely needed. A theme that needs a font face bundles it as Focus does with Source Serif 4: woff2 files and the licence are listed with their SHA-256 in `scripts/download_offline_deps.py`, land in `static/vendor/fonts/<name>/`, and are declared with `@font-face` (`font-display: swap`) in the theme's own stylesheet, so a browser fetches them only while the theme is active.
4. **Slots.** For each slot the design changes, create `templates/themes/<id>/<slot>.html`, starting from a copy of `templates/themes/default/<slot>.html`. Keep the Vue bindings; add and restyle around them. Tailwind classes used in the partial are compiled because `tailwind.config.js` scans `templates/**/*.html`; run `scripts/build_css.sh` after adding new ones.
5. **Guard test.** Run `tests/test_ui_theme_slots.py`. Every dropped behaviour is listed; restore it, or add it to `ALLOWED` or `MOVED` with the reason.
6. **Preview.** Save a screenshot of the theme with the fictional demo data as `static/img/themes/<id>.webp`, 480 by 300 pixels (16:10), light mode, blue scheme. Never use real recordings.
7. **Make it available.** Set `available=True`. The theme's card then appears in the picker, the id is accepted by the endpoint, and the slot branches are rendered.
8. **Screenshots.** Start a demo instance with the fictional seed, choose the theme from Account > Preferences > Appearance as the demo user, and take the fixed screenshot set in light and dark mode. Compare with the default theme's set to check that nothing outside the design changed, and check every color scheme in both modes.

## Testing a theme switch by hand

In the browser console of any signed-in page:

```js
window.speakrApplyUiTheme('<id>');   // attribute, stylesheet, slots, saved to the account
document.documentElement.dataset.uiTheme;
window.speakrApplyUiTheme('default');
```
