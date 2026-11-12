"""New accounts start with the UI theme and color scheme set for the install.

The defaults are the System Settings default_ui_theme and
default_color_scheme, which fall back to DEFAULT_UI_THEME and
DEFAULT_COLOR_SCHEME in .env, and to cards and emerald without either. Every
place that creates an account writes them on the account. Accounts that
already exist keep their values, and NULL still means the default theme and
blue. Signed-out pages show the defaults in a browser with no saved choice.

SHARED-DB: the test users are removed and the two settings restored afterwards.
"""

import contextlib
import json
import logging
import os
import re
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
from src.models import SystemSetting, User  # noqa: E402
from src.config import ui_themes  # noqa: E402
from src.api import auth as auth_mod  # noqa: E402
from src.auth import sso  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "Passw0rd!long-enough"
_ENV = ("DEFAULT_UI_THEME", "DEFAULT_COLOR_SCHEME")
_KEYS = ("default_ui_theme", "default_color_scheme")


@pytest.fixture(autouse=True)
def install_defaults():
    """No .env values and no saved settings unless a test sets them."""
    saved_env = {k: os.environ.pop(k, None) for k in _ENV}
    with app.app_context():
        saved_rows = {}
        for key in _KEYS:
            row = SystemSetting.query.filter_by(key=key).first()
            saved_rows[key] = (row is not None, row.value if row else None)
            if row:
                row.value = None
        db.session.commit()
    ui_themes._warned.clear()
    yield
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    with app.app_context():
        for key, (existed, value) in saved_rows.items():
            row = SystemSetting.query.filter_by(key=key).first()
            if not existed and row:
                db.session.delete(row)
            elif row:
                row.value = value
        db.session.commit()


@contextlib.contextmanager
def env(**values):
    for k, v in values.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k in values:
            os.environ.pop(k, None)


def _setting(key, value):
    with app.app_context():
        SystemSetting.set_setting(key, value, setting_type="string")


@pytest.fixture
def cleanup():
    created = []
    yield created
    with app.app_context():
        for u in User.query.filter(db.or_(User.email.in_(created or [""]),
                                          User.username.in_(created or [""]))).all():
            db.session.delete(u)
        db.session.commit()


def _look(**by):
    with app.app_context():
        u = User.query.filter_by(**by).first()
        assert u is not None, by
        return (u.ui_theme, u.theme_color_scheme, u.theme_dark_mode)


def _user(cleanup, **fields):
    s = uuid.uuid4().hex[:8]
    with app.app_context():
        u = User(username=f"nt_{s}", email=f"nt_{s}@local.test", password="x", **fields)
        db.session.add(u)
        db.session.commit()
        cleanup.append(u.email)
        return u.id


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
    return client


def _defaults(html):
    m = re.search(r"window\.speakrThemeDefaults = (\{.*?\});", html)
    return json.loads(m.group(1)) if m else None


# ---------------------------------------------------------------- the setting


def test_the_install_default_is_cards_and_emerald():
    with app.app_context():
        assert (ui_themes.new_user_theme(), ui_themes.new_user_color_scheme()) == ("cards", "emerald")


def test_the_env_variables_set_the_default():
    with env(DEFAULT_UI_THEME="focus", DEFAULT_COLOR_SCHEME=" Teal "), app.app_context():
        assert (ui_themes.new_user_theme(), ui_themes.new_user_color_scheme()) == ("focus", "teal")


def test_the_standard_look_can_be_the_default():
    with env(DEFAULT_UI_THEME="default", DEFAULT_COLOR_SCHEME="blue"), app.app_context():
        assert (ui_themes.new_user_theme(), ui_themes.new_user_color_scheme()) == ("default", "blue")


@pytest.mark.parametrize("theme,scheme", [("neon", "red"), ("", ""), ("<x>", "blue; x")])
def test_invalid_values_fall_back_with_a_warning(theme, scheme, caplog):
    with env(DEFAULT_UI_THEME=theme, DEFAULT_COLOR_SCHEME=scheme), app.app_context(), \
            caplog.at_level(logging.WARNING, logger="src.config.ui_themes"):
        assert (ui_themes.new_user_theme(), ui_themes.new_user_color_scheme()) == ("default", "blue")
    text = caplog.text
    assert "DEFAULT_UI_THEME" in text and "DEFAULT_COLOR_SCHEME" in text


def test_the_warning_is_logged_once(caplog):
    with env(DEFAULT_UI_THEME="neon"), app.app_context(), \
            caplog.at_level(logging.WARNING, logger="src.config.ui_themes"):
        for _ in range(3):
            ui_themes.new_user_theme()
    assert caplog.text.count("DEFAULT_UI_THEME") == 1


def test_a_system_setting_wins_over_the_env_variable():
    _setting("default_ui_theme", "desk")
    _setting("default_color_scheme", "amber")
    with env(DEFAULT_UI_THEME="focus", DEFAULT_COLOR_SCHEME="teal"), app.app_context():
        assert (ui_themes.new_user_theme(), ui_themes.new_user_color_scheme()) == ("desk", "amber")


def test_the_settings_are_env_backed_and_listed_for_admins(cleanup):
    from src.utils.system_settings import ENV_BACKED_SETTINGS
    assert ENV_BACKED_SETTINGS["default_ui_theme"][:2] == ("DEFAULT_UI_THEME", "cards")
    assert ENV_BACKED_SETTINGS["default_color_scheme"][:2] == ("DEFAULT_COLOR_SCHEME", "emerald")
    admin = _user(cleanup, is_admin=True)
    rows = {r["key"]: r for r in _client(admin).get("/admin/settings").get_json()}
    assert rows["default_ui_theme"]["value"] == "cards" and rows["default_ui_theme"]["source"] == "env"
    assert rows["default_color_scheme"]["value"] == "emerald"


@pytest.mark.parametrize("key,value", [("default_ui_theme", "neon"), ("default_color_scheme", "red")])
def test_admins_cannot_save_an_invalid_default(cleanup, key, value):
    admin = _user(cleanup, is_admin=True)
    resp = _client(admin).post("/admin/settings", json={"key": key, "value": value, "setting_type": "string"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == f"invalid_{key}"


def test_admins_can_save_a_valid_default(cleanup):
    admin = _user(cleanup, is_admin=True)
    client = _client(admin)
    assert client.post("/admin/settings", json={"key": "default_ui_theme", "value": "Focus",
                                                "setting_type": "string"}).status_code == 200
    with app.app_context():
        assert SystemSetting.get_setting("default_ui_theme") == "focus"
        assert ui_themes.new_user_theme() == "focus"


# ---------------------------------------------------------------- every way an account is created


def _register(email):
    with mock.patch.object(auth_mod, "registration_enabled", return_value=True), \
            mock.patch.object(auth_mod, "is_registration_domain_allowed", return_value=True), \
            mock.patch.object(auth_mod, "is_email_verification_enabled", return_value=False):
        return app.test_client().post("/register", data={
            "username": "nt_" + uuid.uuid4().hex[:10], "email": email,
            "password": PW, "confirm_password": PW,
        })


def test_registration_writes_the_defaults(cleanup):
    email = f"reg_{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    assert _register(email).status_code == 302
    assert _look(email=email) == ("cards", "emerald", None)


def test_registration_follows_the_env_variables(cleanup):
    email = f"reg_{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    with env(DEFAULT_UI_THEME="workspace", DEFAULT_COLOR_SCHEME="purple"):
        assert _register(email).status_code == 302
    assert _look(email=email) == ("workspace", "purple", None)


def test_an_admin_created_user_gets_the_defaults(cleanup):
    admin = _user(cleanup, is_admin=True)
    name = "nt_" + uuid.uuid4().hex[:10]
    cleanup.append(name)
    resp = _client(admin).post("/admin/users", json={"username": name, "email": f"{name}@local.test",
                                                     "password": PW})
    assert resp.status_code in (200, 201), resp.data
    assert _look(username=name) == ("cards", "emerald", None)


def test_an_admin_created_user_follows_the_system_setting(cleanup):
    _setting("default_color_scheme", "rose")
    admin = _user(cleanup, is_admin=True)
    name = "nt_" + uuid.uuid4().hex[:10]
    cleanup.append(name)
    _client(admin).post("/admin/users", json={"username": name, "email": f"{name}@local.test", "password": PW})
    assert _look(username=name) == ("cards", "rose", None)


def _sso_env():
    return mock.patch.dict(os.environ, {"SSO_AUTO_REGISTER": "true", "SSO_ALLOWED_DOMAINS": ""})


def test_an_sso_first_sign_in_gets_the_defaults(cleanup):
    sub = "sub-" + uuid.uuid4().hex
    email = f"sso_{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    with _sso_env(), app.app_context():
        sso.create_or_update_sso_user({"sub": sub, "email": email, "email_verified": True,
                                        "preferred_username": "nt_sso"})
    assert _look(sso_subject=sub) == ("cards", "emerald", None)


def test_sso_linking_an_existing_account_keeps_its_look(cleanup):
    uid = _user(cleanup)
    with app.app_context():
        email = db.session.get(User, uid).email
    sub = "sub-" + uuid.uuid4().hex
    with _sso_env(), app.app_context():
        sso.create_or_update_sso_user({"sub": sub, "email": email, "email_verified": True})
        sso.create_or_update_sso_user({"sub": sub, "email": email, "email_verified": True})
    assert _look(id=uid) == (None, None, None)


def _admin_env(work, **extra):
    env_vars = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env_vars.update({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{work}/t.db",
        "UPLOAD_FOLDER": f"{work}/uploads",
        "SECRET_KEY": "admin-test",
        "TEXT_MODEL_API_KEY": "test-key",
        "TRANSCRIPTION_API_KEY": "test-key",
        "TRANSCRIPTION_BASE_URL": "https://api.openai.com/v1",
        "DISABLE_VOICE_EMBEDDING_CHECK": "true",
        "ENABLE_AUTO_PROCESSING": "false",
        "SKIP_EMAIL_DOMAIN_CHECK": "true",
    })
    env_vars.update(extra)
    subprocess.run([sys.executable, "-c", "from src.app import app, db; app.app_context().push(); db.create_all()"],
                   cwd=ROOT, env=env_vars, check=True, capture_output=True, timeout=300)
    return env_vars


def _db_look(work, username):
    con = sqlite3.connect(f"{work}/t.db")
    try:
        return con.execute("SELECT ui_theme, theme_color_scheme FROM user WHERE username = ?",
                           (username,)).fetchone()
    finally:
        con.close()


@pytest.mark.parametrize("extra,expected", [({}, ("cards", "emerald")),
                                            ({"DEFAULT_UI_THEME": "desk", "DEFAULT_COLOR_SCHEME": "teal"},
                                             ("desk", "teal"))])
def test_the_admin_from_the_environment_gets_the_defaults(extra, expected):
    work = tempfile.mkdtemp(prefix="speakr-newtheme-")
    try:
        env_vars = _admin_env(work, ADMIN_USERNAME="admin", ADMIN_EMAIL="admin@example.com",
                              ADMIN_PASSWORD="changeme-please", **extra)
        done = subprocess.run([sys.executable, "scripts/docker_create_admin.py"], cwd=ROOT, env=env_vars,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
        assert _db_look(work, "admin") == expected
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_interactive_admin_script_writes_the_defaults():
    work = tempfile.mkdtemp(prefix="speakr-newtheme-")
    try:
        env_vars = _admin_env(work)
        answers = "localadmin\nadmin@example.com\n"
        code = ("import getpass, scripts.create_admin as m; "
                "getpass.getpass = lambda prompt='': 'changeme-please'; m.create_admin_user()")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env_vars, input=answers,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout[-800:] + done.stderr[-800:]
        assert _db_look(work, "localadmin") == ("cards", "emerald")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_every_user_construction_applies_the_defaults():
    """A new place that creates accounts has to call apply_new_user_theme."""
    missing = []
    for top in ("src", "scripts"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if not name.endswith(".py") or dirpath.endswith(os.path.join("src", "models")):
                    continue
                path = os.path.join(dirpath, name)
                text = open(path, encoding="utf-8").read()
                if re.search(r"(?<![\w.])User\(", text) and "apply_new_user_theme(" not in text:
                    missing.append(os.path.relpath(path, ROOT))
    assert missing == []


# ---------------------------------------------------------------- existing accounts


def test_an_existing_account_keeps_null_values(cleanup):
    uid = _user(cleanup)
    client = _client(uid)
    for path in ("/", "/account"):
        assert client.get(path).status_code == 200
    assert client.post("/api/user/preferences", json={"language": "en"}).status_code == 200
    assert _look(id=uid) == (None, None, None)


def test_an_existing_account_keeps_its_saved_values(cleanup):
    uid = _user(cleanup, ui_theme="focus", theme_color_scheme="rose", theme_dark_mode=True)
    _client(uid).get("/")
    assert _look(id=uid) == ("focus", "rose", True)


def test_null_still_resolves_to_the_default_theme_after_sign_in(cleanup):
    uid = _user(cleanup)
    page = _client(uid).get("/").get_data(as_text=True)
    # The account's values, and what NULL means for them: the default theme and blue.
    assert re.search(r"var saved = (\{.*?\});", page).group(1) == \
        json.dumps({"dark": None, "scheme": None, "ui_theme": None})
    # With nothing in the browser either, the page shows blue and the default
    # theme, not the look for new accounts.
    assert _defaults(page) == {"colorScheme": "blue", "uiTheme": "default"}
    # Only a saved value is written; without one the browser keeps its own choice.
    assert "if (saved.scheme) localStorage.setItem('colorScheme', saved.scheme);" in page
    assert "if (saved.ui_theme) localStorage.setItem('uiTheme', saved.ui_theme);" in page
    head = page.split("</head>", 1)[0]
    assert head.index("window.speakrThemeDefaults =") < head.index("var saved =") < head.index("var themes = ")


# ---------------------------------------------------------------- signed-out pages


SIGNED_OUT = ("/login", "/register", "/forgot-password")


def _signed_out_page(path):
    with mock.patch.object(auth_mod, "registration_enabled", return_value=True), \
            mock.patch.object(auth_mod, "is_smtp_configured", return_value=True):
        resp = app.test_client().get(path)
    assert resp.status_code == 200, path
    return resp.get_data(as_text=True)


@pytest.mark.parametrize("path", SIGNED_OUT)
def test_signed_out_pages_use_the_install_default(path):
    page = _signed_out_page(path)
    assert _defaults(page) == {"colorScheme": "emerald", "uiTheme": "cards"}
    head = page.split("</head>", 1)[0]
    # Before anything reads the theme, and nothing is saved for a visitor.
    assert head.index("window.speakrThemeDefaults =") < head.index("localStorage.getItem('colorScheme')")
    assert head.index("window.speakrThemeDefaults =") < head.index("var themes = ")
    assert "window.speakrSaveTheme =" not in page and "var saved =" not in page
    assert "(window.speakrThemeDefaults || {}).uiTheme" in head


def test_signed_out_pages_follow_the_env_variables():
    with env(DEFAULT_UI_THEME="classic", DEFAULT_COLOR_SCHEME="amber"):
        assert _defaults(_signed_out_page("/login")) == {"colorScheme": "amber", "uiTheme": "classic"}


def test_signed_out_pages_fall_back_on_invalid_values():
    with env(DEFAULT_UI_THEME="neon", DEFAULT_COLOR_SCHEME="red"):
        assert _defaults(_signed_out_page("/login")) == {"colorScheme": "blue", "uiTheme": "default"}


def test_every_page_defines_the_defaults_first():
    templates = os.path.join(ROOT, "templates")
    for dirpath, _, files in os.walk(templates):
        for name in files:
            text = open(os.path.join(dirpath, name), encoding="utf-8").read()
            if "</head>" not in text:
                continue
            head = text.split("</head>", 1)[0]
            rel = os.path.relpath(os.path.join(dirpath, name), templates)
            assert "{% include 'includes/theme_account.html' %}" in head, rel
            first = head.index("{% include 'includes/theme_account.html' %}")
            assert "<script" not in head[:first], rel
            # No reader falls back to blue alone.
            assert "localStorage.getItem('colorScheme') || 'blue'" not in text, rel
