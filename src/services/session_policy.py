"""Session idle timeout, and the warning about a session cookie without Secure.

**Idle timeout.** With session_idle_timeout_minutes (SESSION_IDLE_TIMEOUT_MINUTES)
above 0, a browser session ends when no request of the user arrived for that
many minutes: the next request is treated as signed out, and the user signs in
again. The time of the last request is kept in the signed session cookie. With
the setting at 0 (the default) nothing is checked and nothing is written to
the session.

* API tokens are not affected: a request without a browser session is not
  looked at.
* The requests a page sends by itself (PASSIVE_PATHS: the notification count,
  the job queue status, the CSRF token refresh) are answered while the session
  is valid and do not count as activity, so an open tab left alone does time
  out.
* The remember-me cookie is not accepted while a timeout is set. It would sign
  the user in again right after the timeout.

**Cookie warning.** SESSION_COOKIE_SECURE defaults to false so that a plain
http installation works. When Speakr is reached over HTTPS, or APP_BASE_URL is
an https address, while the cookie is not marked Secure, a warning is logged:
once at startup for APP_BASE_URL, and once per process for the first HTTPS
request. The default is not changed.
"""

import logging
import time

from flask import g, request, session

logger = logging.getLogger(__name__)

LAST_ACTIVITY_KEY = '_last_activity'
# The stored time is renewed at most this often, so that most responses do
# not carry a new session cookie.
RENEW_AFTER_SECONDS = 30
PASSIVE_PATHS = frozenset({
    '/api/notifications/count',
    '/api/recordings/job-queue-status',
    '/api/csrf-token',
})

_https_cookie_warned = False


def idle_timeout_minutes():
    """session_idle_timeout_minutes; 0 or less means no idle timeout."""
    from src.models import SystemSetting
    from src.utils.system_settings import ENV_BACKED_SETTINGS, env_fallback
    env_var, default = ENV_BACKED_SETTINGS['session_idle_timeout_minutes'][:2]
    fallback = env_fallback(env_var, default)
    try:
        value = SystemSetting.get_setting('session_idle_timeout_minutes', fallback)
        return max(0, int(fallback if value is None else value))
    except Exception:
        return max(0, int(fallback or 0))


def _enforce_idle_timeout():
    if not request.endpoint or request.endpoint == 'static':
        return
    from flask import current_app
    remember_name = current_app.config.get('REMEMBER_COOKIE_NAME', 'remember_token')
    has_session = bool(session.get('_user_id'))
    if not has_session and remember_name not in request.cookies:
        return  # a token request or an anonymous one: the setting is not read
    minutes = idle_timeout_minutes()
    if minutes <= 0:
        return
    if not has_session:
        # Flask-Login then ignores the cookie in this request and removes it.
        session['_remember'] = 'clear'
        return
    now = int(time.time())
    last = session.get(LAST_ACTIVITY_KEY)
    if isinstance(last, (int, float)) and not isinstance(last, bool) and now - last > minutes * 60:
        user_id = session.get('_user_id')
        from flask_login import logout_user
        logout_user()
        session.pop(LAST_ACTIVITY_KEY, None)
        g.session_idle_expired = True
        logger.info(f"Session of user {user_id} ended: no request for more than {minutes} minutes")
        return
    if request.path in PASSIVE_PATHS and last is not None:
        return
    if not isinstance(last, (int, float)) or now - last >= RENEW_AFTER_SECONDS:
        session[LAST_ACTIVITY_KEY] = now


def _warn_https_without_secure_cookie():
    global _https_cookie_warned
    if _https_cookie_warned:
        return
    from flask import current_app
    if current_app.config.get('SESSION_COOKIE_SECURE'):
        _https_cookie_warned = True
        return
    if request.is_secure:
        _https_cookie_warned = True
        logger.warning(
            "This request arrived over HTTPS, but the session cookie is not marked Secure. "
            "Set SESSION_COOKIE_SECURE=true so that the cookie is never sent over plain http.")


def secure_cookie_startup_warning(config, base_url):
    """The warning for an https APP_BASE_URL with SESSION_COOKIE_SECURE off, or None."""
    from urllib.parse import urlparse
    if config.get('SESSION_COOKIE_SECURE') or not base_url:
        return None
    if urlparse(base_url).scheme.lower() != 'https':
        return None
    return ("APP_BASE_URL is an https address, but the session cookie is not marked Secure. "
            "Set SESSION_COOKIE_SECURE=true so that the cookie is never sent over plain http.")


def init_app(app):
    """Register the hooks. The idle check runs before every other request hook."""
    hooks = app.before_request_funcs.setdefault(None, [])
    if _enforce_idle_timeout not in hooks:
        hooks.insert(0, _enforce_idle_timeout)
    if _warn_https_without_secure_cookie not in hooks:
        hooks.append(_warn_https_without_secure_cookie)
    from src.services.email import get_app_base_url
    warning = secure_cookie_startup_warning(app.config, get_app_base_url())
    if warning:
        app.logger.warning(warning)
