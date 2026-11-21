"""Language models without an API key (#403).

With neither a key nor an explicit server address, no model is configured:
no client is created, so nothing (no transcript) is sent to the default
hosted address, and model steps report that no model is configured.

A local server (Ollama, vLLM, llama.cpp) often runs without a key. Every
model call refused to run when TEXT_MODEL_API_KEY was empty, although the
client was built with a placeholder for exactly this case, and the chat
client was never created at all. A dedicated chat model was only used when
it had its own key.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services import llm, llm_config

ENV_NAMES = (
    'TEXT_MODEL_API_KEY', 'TEXT_MODEL_BASE_URL', 'TEXT_MODEL_NAME', 'TEXT_MODEL_EXTRA_BODY',
    'CHAT_MODEL_API_KEY', 'CHAT_MODEL_BASE_URL', 'CHAT_MODEL_NAME', 'CHAT_MODEL_EXTRA_BODY',
    'TRANSLATION_MODEL_API_KEY', 'TRANSLATION_MODEL_BASE_URL', 'TRANSLATION_MODEL_NAME',
    'TRANSLATION_MODEL_EXTRA_BODY',
)


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def env(monkeypatch):
    """The environment of a chat role, and a client built from the live config.

    The client is the one the request paths use (``llm_config.get_client``),
    rebuilt whenever the configuration it was built from changes.
    """
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm_config, 'OpenAI', _FakeOpenAI)

    def configure(text_key=None, text_url="http://llm.lan/v1", chat_key=None, chat_url=None, chat_name=None):
        monkeypatch.setenv('TEXT_MODEL_API_KEY', text_key or '')
        monkeypatch.setenv('TEXT_MODEL_BASE_URL', text_url or '')
        monkeypatch.setenv('TEXT_MODEL_NAME', 'text-model')
        monkeypatch.setenv('CHAT_MODEL_API_KEY', chat_key or '')
        monkeypatch.setenv('CHAT_MODEL_BASE_URL', chat_url or '')
        monkeypatch.setenv('CHAT_MODEL_NAME', chat_name or '')
        llm_config.clear_client_cache()
        return llm_config

    llm_config.clear_client_cache()
    return configure


def test_without_any_key_chat_uses_the_text_client(env):
    config = env()
    assert config.get_client('chat') is config.get_client('text')
    assert llm.get_chat_config()["model_name"] == "text-model"


def test_a_keyless_chat_server_gets_its_own_client(env):
    config = env(chat_url="http://chat.lan/v1", chat_name="chat-model")
    chat = config.get_client('chat')
    assert isinstance(chat, _FakeOpenAI)
    assert chat.kwargs["base_url"] == "http://chat.lan/v1" and chat.kwargs["api_key"] == "not-needed"
    assert llm.get_chat_config()["model_name"] == "chat-model"


def test_the_text_key_never_goes_to_another_server(env):
    config = env(text_key="sk-text", text_url="https://openrouter.ai/api/v1",
                 chat_url="http://chat.lan/v1", chat_name="chat-model")
    assert llm.get_chat_config()["api_key"] is None
    assert config.get_client('chat').kwargs["api_key"] == "not-needed"


def test_a_chat_model_on_the_text_server_shares_the_key_and_client(env):
    config = env(text_key="sk-text", chat_key="sk-text", chat_name="chat-model")
    assert llm.get_chat_config()["api_key"] == "sk-text"
    assert config.get_client('chat') is config.get_client('text')


def test_a_chat_model_with_its_own_key(env):
    config = env(text_key="sk-text", chat_key="sk-chat", chat_name="chat-model")
    chat = config.get_client('chat')
    assert chat.kwargs["api_key"] == "sk-chat" and chat.kwargs["base_url"] == "http://llm.lan/v1"


def test_a_chat_name_alone_keeps_the_text_model(env):
    env(text_key="sk-text", chat_name="chat-model")
    assert llm.get_chat_config()["model_name"] == "text-model"


_PROBE = (
    "import json, src.services.llm as l; "
    "print(json.dumps([l.TEXT_MODEL_CONFIGURED, l.client is not None, l.chat_client is not None]))"
)


def _clients(**env):
    import json
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("TEXT_MODEL_", "CHAT_MODEL_"))}
    clean.update(env)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=root, env=clean,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("env,expected", [
    ({}, [False, False, False]),                                              # nothing set: nothing is sent
    ({"TEXT_MODEL_BASE_URL": "http://llm.lan/v1"}, [True, True, True]),       # local server, no key
    ({"TEXT_MODEL_API_KEY": "sk-x"}, [True, True, True]),                     # hosted default with a key
    ({"CHAT_MODEL_NAME": "c", "CHAT_MODEL_BASE_URL": "http://chat.lan/v1"}, [False, False, True]),
])
def test_a_model_is_configured_only_when_set(env, expected):
    assert _clients(**env) == expected


def test_without_a_model_the_call_says_so(monkeypatch):
    monkeypatch.setattr(llm, "client", None)
    with pytest.raises(ValueError, match="No text model is configured"):
        llm.call_llm_completion([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize('configured,expected', [(True, 'isAutoIdentifying || false'), (False, 'isAutoIdentifying || true')])
def test_identify_from_conversation_is_disabled_without_a_model(monkeypatch, configured, expected):
    """The button reads names from the conversation with the text model; voice
    matches need no model and appear as suggestions anyway."""
    import secrets

    from src.app import app, db
    from src.models import User
    from src.services import llm_config

    monkeypatch.setattr(llm_config, 'is_configured', lambda role: configured and role == 'text')
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f'idb_{s}', email=f'idb_{s}@local.test', password='x')
        db.session.add(u)
        db.session.commit()
        try:
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['_user_id'] = str(u.id)
            page = client.get('/')
            assert page.status_code == 200
            assert expected in page.get_data(as_text=True)
        finally:
            db.session.delete(u)
            db.session.commit()
