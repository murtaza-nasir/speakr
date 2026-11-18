"""Defaults for user settings (#584).

An administrator sets a default for a list of user settings
(src/services/user_defaults.py). Every place that creates an account writes
the defaults on it. When a default is changed, the accounts that still have
the previous default can get the new one in the same request
(apply_to_existing on POST /admin/settings); an account with another value is
left alone, and without the parameter no account changes. GET
/admin/user-defaults/<key>/accounts gives the number of accounts on the
default in effect. Each change is recorded in the audit log with the key, the
old and the new default and the number of accounts updated.

SHARED-DB: the test users are removed and the settings restored afterwards.
The tests that update accounts use values that no other account has (a color
scheme set as the default first, rare time zones), so accounts of other tests
are not touched; NULL handling is checked on the test users only.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import AuditEvent, SystemSetting, User  # noqa: E402
from src.api import auth as auth_mod  # noqa: E402
from src.auth import sso  # noqa: E402
from src.config import ui_themes  # noqa: E402
from src.services import user_defaults  # noqa: E402
from src.utils.system_settings import ENV_BACKED_SETTINGS  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "Passw0rd!long-enough"
NEW_KEYS = ("default_ui_language", "default_transcription_language", "default_output_language",
            "default_timezone_mode", "default_auto_summarization", "default_auto_speaker_labelling",
            "default_extract_events")
LOOK_KEYS = ("default_ui_theme", "default_color_scheme", "default_audio_player_position")
COLUMNS = ("ui_theme", "theme_color_scheme", "audio_player_position", "ui_language", "transcription_language",
           "output_language", "timezone_mode", "timezone", "auto_summarization", "auto_speaker_labelling",
           "extract_events")
BUILT_IN = ("cards", "emerald", "top", "en", None, None, "auto", None, True, False, False)
# One value of every default that differs from the built-in one.
CHOSEN = {
    "default_ui_theme": "desk", "default_color_scheme": "teal", "default_audio_player_position": "bottom",
    "default_ui_language": "de", "default_transcription_language": "fr", "default_output_language": "German",
    "default_timezone_mode": "fixed", "default_timezone": "Pacific/Chatham",
    "default_auto_summarization": "false", "default_auto_speaker_labelling": "true",
    "default_extract_events": "true",
}
CHOSEN_ROW = ("desk", "teal", "bottom", "de", "fr", "German", "fixed", "Pacific/Chatham", False, True, True)


@pytest.fixture(autouse=True)
def install_defaults():
    """No .env values and no saved defaults unless a test sets them."""
    env_names = [ENV_BACKED_SETTINGS[key][0] for key in user_defaults.KEYS if key in ENV_BACKED_SETTINGS]
    saved_env = {name: os.environ.pop(name, None) for name in env_names}
    with app.app_context():
        saved_rows = {}
        for key in user_defaults.KEYS:
            row = SystemSetting.query.filter_by(key=key).first()
            saved_rows[key] = (row is not None, row.value if row else None)
            if row:
                row.value = "UTC" if key == "default_timezone" else None
        db.session.commit()
    ui_themes._warned.clear()
    yield
    for name, value in saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    with app.app_context():
        for key, (existed, value) in saved_rows.items():
            row = SystemSetting.query.filter_by(key=key).first()
            if not existed and row:
                db.session.delete(row)
            elif row:
                row.value = value
        db.session.commit()


@pytest.fixture
def cleanup():
    created = []
    yield created
    with app.app_context():
        for u in User.query.filter(db.or_(User.email.in_(created or [""]),
                                          User.username.in_(created or [""]))).all():
            db.session.delete(u)
        db.session.commit()


def _save(values):
    with app.app_context():
        for key, value in values.items():
            default = user_defaults.get(key)
            SystemSetting.set_setting(key, value, setting_type=default.setting_type)


def _user(cleanup, **fields):
    """An account as it is stored, with NULL for every field given as None."""
    s = uuid.uuid4().hex[:8]
    with app.app_context():
        u = User(username=f"ud_{s}", email=f"ud_{s}@local.test", password="x",
                 is_admin=fields.pop("is_admin", False))
        db.session.add(u)
        db.session.commit()
        cleanup.append(u.email)
        if fields:
            User.query.filter_by(id=u.id).update(fields)
            db.session.commit()
        return u.id


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    return client


def _row(**by):
    with app.app_context():
        u = User.query.filter_by(**by).first()
        assert u is not None, by
        return tuple(getattr(u, column) for column in COLUMNS)


def _value(uid, column):
    with app.app_context():
        return getattr(db.session.get(User, uid), column)


def _post(client, key, value, **extra):
    return client.post("/admin/settings", json={"key": key, "value": value, **extra})


# ---------------------------------------------------------------- the list of defaults


def test_every_default_is_a_setting_with_a_valid_built_in_value():
    assert set(LOOK_KEYS + NEW_KEYS + ("default_timezone",)) == set(user_defaults.KEYS)
    for key in NEW_KEYS:
        assert key in ENV_BACKED_SETTINGS, key
        default = user_defaults.get(key)
        env_var, built_in, setting_type, _desc = ENV_BACKED_SETTINGS[key]
        assert (default.env, default.setting_type) == (env_var, setting_type), key
        expected = ("true" if built_in else "false") if isinstance(built_in, bool) else built_in
        assert default.builtin == expected and default.clean(default.builtin) == default.builtin, key
    assert user_defaults.get("allow_registration") is None and user_defaults.get(None) is None


def test_the_built_in_defaults_are_the_values_of_an_account_before_the_feature():
    with app.app_context():
        u = user_defaults.apply_new_user_defaults(User(username="x", email="x@local.test"))
        assert tuple(getattr(u, column) for column in COLUMNS) == BUILT_IN


def test_the_env_variables_set_the_defaults():
    values = {"DEFAULT_UI_LANGUAGE": "pt", "DEFAULT_TRANSCRIPTION_LANGUAGE": "ES",
              "DEFAULT_OUTPUT_LANGUAGE": "spanish", "DEFAULT_TIMEZONE_MODE": "fixed",
              "DEFAULT_AUTO_SUMMARIZATION": "false", "DEFAULT_AUTO_SPEAKER_LABELLING": "true",
              "DEFAULT_EXTRACT_EVENTS": "true"}
    with mock.patch.dict(os.environ, values), app.app_context():
        u = user_defaults.apply_new_user_defaults(User(username="x", email="x@local.test"))
        assert tuple(getattr(u, column) for column in COLUMNS[3:]) == \
            ("pt-BR", "es", "Spanish", "fixed", "UTC", False, True, True)


def test_an_invalid_env_value_falls_back_to_the_built_in_value():
    values = {"DEFAULT_UI_LANGUAGE": "xx", "DEFAULT_TRANSCRIPTION_LANGUAGE": "klingon",
              "DEFAULT_OUTPUT_LANGUAGE": "Klingon", "DEFAULT_TIMEZONE_MODE": "sometimes"}
    with mock.patch.dict(os.environ, values), app.app_context():
        u = user_defaults.apply_new_user_defaults(User(username="x", email="x@local.test"))
        assert tuple(getattr(u, column) for column in COLUMNS) == BUILT_IN


def test_a_system_setting_wins_over_the_env_variable():
    _save({"default_ui_language": "fr"})
    with mock.patch.dict(os.environ, {"DEFAULT_UI_LANGUAGE": "de"}), app.app_context():
        assert user_defaults.get("default_ui_language").current() == "fr"


def test_admins_see_the_defaults_with_their_values_and_lists(cleanup):
    _save({"default_output_language": "German"})
    admin = _user(cleanup, is_admin=True)
    resp = _client(admin).get("/admin/user-defaults")
    assert resp.status_code == 200
    rows = {row["key"]: row for row in resp.get_json()["defaults"]}
    assert list(rows) == list(user_defaults.KEYS)
    assert rows["default_ui_theme"]["value"] == "cards" and "desk" in rows["default_ui_theme"]["choices"]
    assert rows["default_ui_theme"]["source"] == "env"
    assert rows["default_output_language"]["value"] == "German"
    assert rows["default_output_language"]["source"] == "setting"
    assert {"value": "German", "code": "de"} in rows["default_output_language"]["options"]
    assert {"value": "pt-BR", "label": "Português (Brasil)"} in rows["default_ui_language"]["options"]
    assert rows["default_auto_summarization"]["setting_type"] == "boolean"
    assert rows["default_auto_summarization"]["value"] == "true"
    assert rows["default_timezone"]["value"] == "UTC"
    assert _client(_user(cleanup)).get("/admin/user-defaults").status_code == 403


def test_the_account_page_lists_the_interface_languages_of_the_shared_list(cleanup):
    from src.utils.localization import UI_LANGUAGES, available_languages
    assert sorted(code for code, _label in UI_LANGUAGES) == sorted(available_languages())
    uid = _user(cleanup, ui_language="de")
    html = _client(uid).get("/account").get_data(as_text=True)
    select = html.split('id="ui_language"', 1)[1].split("</select>", 1)[0]
    for code, label in UI_LANGUAGES:
        assert f'<option value="{code}" {"selected" if code == "de" else ""}>{label}</option>' in select, code


def test_the_new_texts_exist_in_every_locale():
    keys = None
    for name in sorted(os.listdir(os.path.join(ROOT, "static", "locales"))):
        if not name.endswith(".json"):
            continue
        data = json.load(open(os.path.join(ROOT, "static", "locales", name), encoding="utf-8"))
        texts = data["adminDashboard"]["userDefaults"]
        keys = keys or set(texts)
        assert set(texts) == keys and all(texts.values()), name
        for key in ("confirmMessage", "confirmMessagePlural"):
            assert "{{count}}" in texts[key] and "{{value}}" in texts[key], (name, key)
        assert "{{count}}" in texts["applyToCount"] and "{{count}}" in texts["applyToCountPlural"], name
        assert "{{count}}" in texts["savedUpdated"], name
        assert "{{allowed}}" in data["server"]["apiError"]["invalid_user_default"], name
        assert data["server"]["apiError"]["unknown_user_default"], name


# ---------------------------------------------------------------- every way an account is created


def test_registration_writes_every_default(cleanup):
    _save(CHOSEN)
    email = f"ud_{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    with mock.patch.object(auth_mod, "registration_enabled", return_value=True), \
            mock.patch.object(auth_mod, "is_registration_domain_allowed", return_value=True), \
            mock.patch.object(auth_mod, "is_email_verification_enabled", return_value=False):
        app.test_client().post("/register", data={
            "username": "ud_" + uuid.uuid4().hex[:10], "email": email,
            "password": PW, "confirm_password": PW})
    assert _row(email=email) == CHOSEN_ROW


def test_an_admin_created_user_gets_every_default(cleanup):
    _save(CHOSEN)
    admin = _user(cleanup, is_admin=True)
    name = "ud_" + uuid.uuid4().hex[:8]
    cleanup.append(name)
    resp = _client(admin).post("/admin/users", json={"username": name, "email": f"{name}@example.com",
                                                     "password": PW})
    assert resp.status_code in (200, 201), resp.data[:300]
    assert _row(username=name) == CHOSEN_ROW


def test_an_sso_first_sign_in_gets_every_default(cleanup):
    _save(CHOSEN)
    sub = "sub-" + uuid.uuid4().hex
    email = f"ud_{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    with mock.patch.dict(os.environ, {"SSO_AUTO_REGISTER": "true", "SSO_ALLOWED_DOMAINS": ""}), app.app_context():
        sso.create_or_update_sso_user({"sub": sub, "email": email, "email_verified": True,
                                        "preferred_username": "ud_sso"})
    assert _row(sso_subject=sub) == CHOSEN_ROW


def test_an_account_without_a_fixed_default_zone_has_no_zone(cleanup):
    """In the automatic mode the browser's zone is saved as the user visits."""
    _save({"default_timezone": "Pacific/Chatham"})
    with app.app_context():
        u = user_defaults.apply_new_user_defaults(User(username="x", email="x@local.test"))
        assert (u.timezone_mode, u.timezone) == ("auto", None)


def _script_env(work, **extra):
    env_vars = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env_vars.update({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{work}/t.db", "UPLOAD_FOLDER": f"{work}/uploads",
        "SECRET_KEY": "admin-test", "TEXT_MODEL_API_KEY": "test-key", "TRANSCRIPTION_API_KEY": "test-key",
        "TRANSCRIPTION_BASE_URL": "https://api.openai.com/v1", "DISABLE_VOICE_EMBEDDING_CHECK": "true",
        "ENABLE_AUTO_PROCESSING": "false", "SKIP_EMAIL_DOMAIN_CHECK": "true",
        "DEFAULT_UI_THEME": "desk", "DEFAULT_UI_LANGUAGE": "de", "DEFAULT_TRANSCRIPTION_LANGUAGE": "fr",
        "DEFAULT_OUTPUT_LANGUAGE": "German", "DEFAULT_AUTO_SUMMARIZATION": "false",
        "DEFAULT_EXTRACT_EVENTS": "true",
    })
    env_vars.update(extra)
    subprocess.run([sys.executable, "-c", "from src.app import app, db; app.app_context().push(); db.create_all()"],
                   cwd=ROOT, env=env_vars, check=True, capture_output=True, timeout=300)
    return env_vars


def _script_row(work, username):
    con = sqlite3.connect(f"{work}/t.db")
    try:
        return con.execute("SELECT ui_theme, ui_language, transcription_language, output_language, "
                           "auto_summarization, extract_events FROM user WHERE username = ?",
                           (username,)).fetchone()
    finally:
        con.close()


def test_the_admin_from_the_environment_gets_every_default():
    work = tempfile.mkdtemp(prefix="speakr-userdefaults-")
    try:
        env_vars = _script_env(work, ADMIN_USERNAME="admin", ADMIN_EMAIL="admin@example.com",
                               ADMIN_PASSWORD="changeme-please")
        done = subprocess.run([sys.executable, "scripts/docker_create_admin.py"], cwd=ROOT, env=env_vars,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
        assert _script_row(work, "admin") == ("desk", "de", "fr", "German", 0, 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_interactive_admin_script_writes_every_default():
    work = tempfile.mkdtemp(prefix="speakr-userdefaults-")
    try:
        env_vars = _script_env(work)
        code = ("import getpass, scripts.create_admin as m; "
                "getpass.getpass = lambda prompt='': 'changeme-please'; m.create_admin_user()")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env_vars,
                              input="localadmin\nadmin@example.com\n", capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
        assert _script_row(work, "localadmin") == ("desk", "de", "fr", "German", 0, 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- accounts on a default


@pytest.mark.parametrize("key,column,value,is_default", [
    # NULL counts where it means the value ...
    ("default_ui_theme", "ui_theme", "default", True),
    ("default_color_scheme", "theme_color_scheme", "blue", True),
    ("default_audio_player_position", "audio_player_position", "bottom", True),
    ("default_ui_language", "ui_language", "en", True),
    ("default_transcription_language", "transcription_language", "auto", True),
    ("default_output_language", "output_language", "auto", True),
    ("default_timezone_mode", "timezone_mode", "auto", True),
    ("default_auto_summarization", "auto_summarization", "true", True),
    ("default_auto_speaker_labelling", "auto_speaker_labelling", "false", True),
    ("default_extract_events", "extract_events", "false", True),
    # ... and for no other value.
    ("default_ui_theme", "ui_theme", "cards", False),
    ("default_color_scheme", "theme_color_scheme", "emerald", False),
    ("default_audio_player_position", "audio_player_position", "top", False),
    ("default_ui_language", "ui_language", "de", False),
    ("default_transcription_language", "transcription_language", "fr", False),
    ("default_output_language", "output_language", "German", False),
    ("default_auto_summarization", "auto_summarization", "false", False),
    ("default_auto_speaker_labelling", "auto_speaker_labelling", "true", False),
    ("default_extract_events", "extract_events", "true", False),
])
def test_null_counts_as_a_default_only_where_null_means_it(cleanup, key, column, value, is_default):
    uid = _user(cleanup, **{column: None})
    with app.app_context():
        default = user_defaults.get(key)
        found = User.query.filter(User.id == uid, default.condition(value)).count()
        assert found == (1 if is_default else 0)


def test_an_account_with_the_stored_value_is_on_the_default(cleanup):
    stored = {"ui_theme": "desk", "theme_color_scheme": "teal", "audio_player_position": "bottom",
              "ui_language": "de", "transcription_language": "fr", "output_language": "German",
              "auto_summarization": False, "auto_speaker_labelling": True, "extract_events": True}
    uid = _user(cleanup, **stored)
    with app.app_context():
        for default in user_defaults.DEFAULTS:
            if default.column not in stored:
                continue
            on = User.query.filter(User.id == uid, default.condition(CHOSEN[default.key])).count()
            off = User.query.filter(User.id == uid, default.condition(default.builtin)).count()
            assert (on, off) == (1, 0), default.key


def test_the_count_is_of_the_accounts_on_the_default_in_effect(cleanup):
    admin = _user(cleanup, is_admin=True, theme_color_scheme="purple")
    client = _client(admin)
    _save({"default_color_scheme": "rose"})
    before = client.get("/admin/user-defaults/default_color_scheme/accounts").get_json()
    assert before["key"] == "default_color_scheme" and before["value"] == "rose"
    _user(cleanup, theme_color_scheme="rose")
    _user(cleanup, theme_color_scheme="rose")
    _user(cleanup, theme_color_scheme="amber")
    _user(cleanup, theme_color_scheme=None)
    after = client.get("/admin/user-defaults/default_color_scheme/accounts").get_json()
    assert after["count"] == before["count"] + 2


def test_the_count_is_for_admins_and_known_defaults(cleanup):
    admin = _user(cleanup, is_admin=True)
    resp = _client(admin).get("/admin/user-defaults/allow_registration/accounts")
    assert resp.status_code == 404 and resp.get_json()["code"] == "unknown_user_default"
    assert _client(_user(cleanup)).get("/admin/user-defaults/default_ui_theme/accounts").status_code == 403


# ---------------------------------------------------------------- a changed default


def test_only_accounts_on_the_previous_default_get_the_new_one(cleanup):
    admin = _user(cleanup, is_admin=True, theme_color_scheme="purple")
    client = _client(admin)
    _save({"default_color_scheme": "rose"})
    on_default = [_user(cleanup, theme_color_scheme="rose") for _ in range(3)]
    other = _user(cleanup, theme_color_scheme="amber")
    empty = _user(cleanup, theme_color_scheme=None)
    count = client.get("/admin/user-defaults/default_color_scheme/accounts").get_json()["count"]
    assert count >= 3
    resp = _post(client, "default_color_scheme", "teal", apply_to_existing=True)
    assert resp.status_code == 200, resp.data[:300]
    assert resp.get_json()["users_updated"] == count and resp.get_json()["value"] == "teal"
    assert [_value(uid, "theme_color_scheme") for uid in on_default] == ["teal"] * 3
    assert _value(other, "theme_color_scheme") == "amber"
    assert _value(empty, "theme_color_scheme") is None
    assert _value(admin, "theme_color_scheme") == "purple"
    with app.app_context():
        assert user_defaults.get("default_color_scheme").current() == "teal"


@pytest.mark.parametrize("extra", [{}, {"apply_to_existing": False}, {"apply_to_existing": "true"}])
def test_without_the_parameter_only_new_accounts_get_the_default(cleanup, extra):
    """As before the feature, also for an API token with admin rights."""
    admin = _user(cleanup, is_admin=True)
    _save({"default_color_scheme": "rose"})
    uid = _user(cleanup, theme_color_scheme="rose")
    resp = _post(_client(admin), "default_color_scheme", "teal", setting_type="string", **extra)
    assert resp.status_code == 200 and resp.get_json()["users_updated"] == 0
    assert _value(uid, "theme_color_scheme") == "rose"
    with app.app_context():
        assert user_defaults.get("default_color_scheme").current() == "teal"


def test_a_switch_default_updates_the_accounts_on_the_previous_one(cleanup):
    admin = _user(cleanup, is_admin=True, extract_events=False)
    _save({"default_extract_events": "true"})
    on_default = _user(cleanup, extract_events=True)
    other = _user(cleanup, extract_events=False)
    empty = _user(cleanup, extract_events=None)
    # Accounts of other tests with event extraction on would be turned off
    # too; they are put back below.
    with app.app_context():
        others = [u.id for u in User.query.filter(User.extract_events.is_(True), User.id != on_default).all()]
    try:
        resp = _post(_client(admin), "default_extract_events", False, apply_to_existing=True)
        assert resp.status_code == 200 and resp.get_json()["users_updated"] == 1 + len(others)
        assert resp.get_json()["value"] == "false" and resp.get_json()["setting_type"] == "boolean"
        assert _value(on_default, "extract_events") is False
        assert _value(other, "extract_events") is False and _value(empty, "extract_events") is None
    finally:
        with app.app_context():
            if others:
                User.query.filter(User.id.in_(others)).update({"extract_events": True}, synchronize_session=False)
                db.session.commit()


def test_clearing_a_default_can_update_accounts_to_the_env_value(cleanup):
    admin = _user(cleanup, is_admin=True)
    _save({"default_color_scheme": "rose"})
    uid = _user(cleanup, theme_color_scheme="rose")
    with mock.patch.dict(os.environ, {"DEFAULT_COLOR_SCHEME": "amber"}):
        resp = _post(_client(admin), "default_color_scheme", "", apply_to_existing=True)
        assert resp.status_code == 200 and resp.get_json()["users_updated"] >= 1
    assert _value(uid, "theme_color_scheme") == "amber"


def test_saving_the_same_default_updates_no_account(cleanup):
    admin = _user(cleanup, is_admin=True)
    _save({"default_color_scheme": "rose"})
    uid = _user(cleanup, theme_color_scheme="rose")
    resp = _post(_client(admin), "default_color_scheme", "rose", apply_to_existing=True)
    assert resp.status_code == 200 and resp.get_json()["users_updated"] == 0
    assert _value(uid, "theme_color_scheme") == "rose"


def test_the_fixed_default_zone_follows_a_change_on_accounts_that_have_it(cleanup):
    admin = _user(cleanup, is_admin=True, timezone_mode="fixed", timezone="Europe/Berlin")
    client = _client(admin)
    _save({"default_timezone": "Pacific/Chatham", "default_timezone_mode": "fixed"})
    on_default = _user(cleanup, timezone_mode="fixed", timezone="Pacific/Chatham")
    own_zone = _user(cleanup, timezone_mode="fixed", timezone="Europe/Berlin")
    automatic = _user(cleanup, timezone_mode="auto", timezone="Pacific/Chatham")
    assert client.get("/admin/user-defaults/default_timezone/accounts").get_json()["count"] == 1
    assert client.get("/admin/user-defaults/default_timezone_mode/accounts").get_json()["count"] == 1

    resp = _post(client, "default_timezone", "Pacific/Niue", apply_to_existing=True)
    assert resp.status_code == 200 and resp.get_json()["users_updated"] == 1
    assert _value(on_default, "timezone") == "Pacific/Niue"
    assert _value(own_zone, "timezone") == "Europe/Berlin"
    assert _value(automatic, "timezone") == "Pacific/Chatham"

    # Back to the automatic mode: the account on the fixed default follows.
    resp = _post(client, "default_timezone_mode", "auto", apply_to_existing=True)
    assert resp.status_code == 200 and resp.get_json()["users_updated"] == 1
    assert (_value(on_default, "timezone_mode"), _value(on_default, "timezone")) == ("auto", "Pacific/Niue")
    assert _value(own_zone, "timezone_mode") == "fixed" and _value(admin, "timezone_mode") == "fixed"

    # In the automatic mode the default zone is on no account.
    assert client.get("/admin/user-defaults/default_timezone/accounts").get_json()["count"] == 0
    resp = _post(client, "default_timezone", "Pacific/Chatham", apply_to_existing=True)
    assert resp.status_code == 200 and resp.get_json()["users_updated"] == 0
    assert _value(on_default, "timezone") == "Pacific/Niue"


def test_the_fixed_mode_gives_automatic_accounts_the_default_zone(cleanup):
    """Checked on the test accounts only: the statement would change every
    automatic account of the shared database."""
    _save({"default_timezone": "Pacific/Chatham"})
    automatic = _user(cleanup, timezone_mode="auto", timezone="Europe/Berlin")
    empty = _user(cleanup, timezone_mode=None)
    fixed = _user(cleanup, timezone_mode="fixed", timezone="Europe/Berlin")
    with app.app_context():
        default = user_defaults.get("default_timezone_mode")
        mine = User.id.in_([automatic, empty, fixed])
        assert {u.id for u in User.query.filter(mine, default.condition("auto")).all()} == {automatic, empty}
        assert default.assignment("fixed") == {"timezone_mode": "fixed", "timezone": "Pacific/Chatham"}
        assert default.assignment("auto") == {"timezone_mode": "auto"}


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("key,value,code", [
    ("default_ui_language", "xx", "invalid_user_default"),
    ("default_transcription_language", "klingon", "invalid_user_default"),
    ("default_output_language", "Klingon", "invalid_user_default"),
    ("default_timezone_mode", "sometimes", "invalid_user_default"),
    ("default_auto_summarization", "maybe", "invalid_user_default"),
    ("default_auto_speaker_labelling", "2", "invalid_user_default"),
    ("default_extract_events", "on", "invalid_user_default"),
    ("default_timezone", "Mars/Olympus", "invalid_timezone_value"),
    ("default_timezone", "", "invalid_timezone_value"),
    ("default_ui_theme", "neon", "invalid_default_ui_theme"),
    ("default_color_scheme", "red", "invalid_default_color_scheme"),
    ("default_audio_player_position", "middle", "invalid_default_audio_player_position"),
])
def test_a_bad_value_is_refused_and_changes_nothing(cleanup, key, value, code):
    admin = _user(cleanup, is_admin=True)
    with app.app_context():
        before = user_defaults.get(key).current()
    resp = _post(_client(admin), key, value, apply_to_existing=True)
    assert resp.status_code == 400 and resp.get_json()["code"] == code, resp.data[:300]
    assert "{{" not in resp.get_json()["error"] and (code != "invalid_user_default" or key in resp.get_json()["error"])
    with app.app_context():
        assert user_defaults.get(key).current() == before


@pytest.mark.parametrize("key,value,stored", [
    ("default_ui_language", "PT", "pt-BR"),
    ("default_transcription_language", "DE", "de"),
    ("default_transcription_language", "auto", "auto"),
    ("default_output_language", "german", "German"),
    ("default_output_language", "AUTO", "auto"),
    ("default_timezone_mode", "Fixed", "fixed"),
    ("default_auto_summarization", "no", "false"),
    ("default_extract_events", True, "true"),
])
def test_a_valid_value_is_stored_in_its_own_spelling(cleanup, key, value, stored):
    admin = _user(cleanup, is_admin=True)
    resp = _post(_client(admin), key, value)
    assert resp.status_code == 200 and resp.get_json()["value"] == stored, resp.data[:300]
    with app.app_context():
        assert user_defaults.get(key).current() == stored


def test_non_admins_cannot_change_a_default(cleanup):
    uid = _user(cleanup)
    assert _post(_client(uid), "default_ui_language", "de", apply_to_existing=True).status_code == 403


# ---------------------------------------------------------------- the audit log


@pytest.fixture
def audit_on():
    """The audit log on, whatever an earlier test file left in its settings."""
    from src.services import audit
    with app.app_context():
        saved = SystemSetting.query.filter_by(key="audit_log_enabled").first()
        saved_value = saved.value if saved else None
        SystemSetting.set_setting("audit_log_enabled", "true", None, "boolean")
        audit.reset_memory()
    yield
    with app.app_context():
        SystemSetting.set_setting("audit_log_enabled", saved_value, None, "boolean")


def test_a_change_is_recorded_with_the_defaults_and_the_number_of_accounts(cleanup, audit_on):
    admin = _user(cleanup, is_admin=True)
    client = _client(admin)
    _save({"default_color_scheme": "rose"})
    _user(cleanup, theme_color_scheme="rose")
    with app.app_context():
        start = db.session.query(db.func.coalesce(db.func.max(AuditEvent.id), 0)).scalar()
    updated = _post(client, "default_color_scheme", "teal", apply_to_existing=True).get_json()["users_updated"]
    assert updated >= 1
    assert _post(client, "default_ui_language", "de").status_code == 200
    assert _post(client, "default_auto_summarization", "false").status_code == 200
    assert _post(client, "default_timezone", "Pacific/Chatham").status_code == 200
    assert _post(client, "default_ui_language", "").status_code == 200
    with app.app_context():
        rows = AuditEvent.query.filter(AuditEvent.id > start, AuditEvent.event == "admin.setting.changed") \
            .order_by(AuditEvent.id).all()
        assert all(row.actor_user_id == admin for row in rows)
        assert [row.details for row in rows] == [
            {"key": "default_color_scheme", "old": "rose", "new": "teal", "users_updated": updated},
            {"key": "default_ui_language", "old": "en", "new": "de", "users_updated": 0},
            {"key": "default_auto_summarization", "old": "true", "new": "false", "users_updated": 0},
            {"key": "default_timezone", "old": "UTC", "new": "Pacific/Chatham", "users_updated": 0},
            {"key": "default_ui_language", "old": "de", "new": "en", "users_updated": 0},
        ]


def test_other_settings_are_recorded_without_a_number_of_accounts(cleanup, audit_on):
    admin = _user(cleanup, is_admin=True)
    with app.app_context():
        start = db.session.query(db.func.coalesce(db.func.max(AuditEvent.id), 0)).scalar()
        saved = SystemSetting.get_setting("max_concurrent_uploads", None)
    try:
        resp = _post(_client(admin), "max_concurrent_uploads", "4", setting_type="integer", apply_to_existing=True)
        assert resp.status_code == 200 and "users_updated" not in resp.get_json()
        with app.app_context():
            row = AuditEvent.query.filter(AuditEvent.id > start, AuditEvent.event == "admin.setting.changed").one()
            assert row.details["key"] == "max_concurrent_uploads" and "users_updated" not in row.details
    finally:
        with app.app_context():
            SystemSetting.set_setting("max_concurrent_uploads", saved, None, "integer")
