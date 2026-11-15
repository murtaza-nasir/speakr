import json, os, sys, uuid

import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from src.app import app
from src.init_db import initialize_database
from src.api import auth as auth_mod

from src.services import transcription_translation as tt


def _fake_call_translating(messages, user_id):
    import json as _j
    content = messages[-1]['content']
    start = content.find('{')
    end = content.rfind('}')
    payload = _j.loads(content[start:end + 1])
    return _j.dumps({k: (v + " [es]" if isinstance(v, str) else v) for k, v in payload.items()}, ensure_ascii=False)


def test_json_segments_alignment(monkeypatch):
    monkeypatch.setattr(tt, '_call_llm', _fake_call_translating)
    text = json.dumps([
        {"speaker": "A", "start_time": 1.0, "end_time": 2.0, "sentence": "Hello world"},
        {"speaker": "B", "start_time": 2.0, "end_time": 3.0, "sentence": "How are you"},
    ])
    out = tt.translate_transcription_text(text, user_id=None, target_language="Spanish")
    data = json.loads(out)
    assert len(data) == 2
    assert data[0]['speaker'] == 'A'
    assert data[0]['start_time'] == 1.0
    assert data[0]['end_time'] == 2.0
    assert data[1]['speaker'] == 'B'
    assert data[1]['start_time'] == 2.0
    assert data[1]['end_time'] == 3.0
    assert data[0]['sentence'].endswith('[es]')
    assert data[1]['sentence'].endswith('[es]')


def test_reject_missing_keys(monkeypatch):
    def fake_call(messages, user_id):
        content = messages[-1]['content']
        import json as _j
        start = content.find('{')
        end = content.rfind('}')
        payload = _j.loads(content[start:end + 1])
        keys = list(payload.keys())
        # Always drop the last key, so even a single sentence comes back empty.
        return _j.dumps({k: "x" for k in keys[:-1]}, ensure_ascii=False)

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    text = json.dumps([{"speaker": "A", "sentence": "Hello"}, {"speaker": "B", "sentence": "Hi"}])
    with pytest.raises(ValueError):
        tt.translate_json_segments(text, user_id=None, target_language="Spanish")


def test_reject_mismatched_keys(monkeypatch):
    def fake_call(messages, user_id):
        content = messages[-1]['content']
        import json as _j
        start = content.find('{')
        end = content.rfind('}')
        payload = _j.loads(content[start:end + 1])
        keys = list(payload.keys())
        result = {keys[0]: "x"}
        result["999"] = "y"
        return _j.dumps(result, ensure_ascii=False)

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    text = json.dumps([{"speaker": "A", "sentence": "Hello"}, {"speaker": "B", "sentence": "Hi"}])
    with pytest.raises(ValueError):
        tt.translate_json_segments(text, user_id=None, target_language="Spanish")


def test_prefix_cache_layout_reuses_the_shared_transcript_prefix(monkeypatch):
    """With prefix caching enabled, translation sends the same system message
    and transcript-first block as title/summary, with the JSON payload as the
    transcript, so the backend can reuse the transcript KV cache."""
    import src.transcription_prompts as prompts

    calls = []

    def fake_call(messages, user_id):
        calls.append(messages)
        return json.dumps({"1": "Hola"})

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    with patch('src.tasks.processing.PREFIX_CACHE_OPTIMIZED_PROMPTS', True):
        out = tt.translate_json_segments(
            json.dumps([{"speaker": "A", "sentence": "Hello"}]),
            user_id=None,
            target_language="Spanish",
        )

    assert json.loads(out)[0]['sentence'] == 'Hola'
    system, user = calls[0]
    payload_json = json.dumps({"1": "Hello"}, ensure_ascii=False)
    prefix = prompts.shared_user_prefix(payload_json)
    assert system['content'] == prompts.SHARED_LLM_SYSTEM_MSG
    assert user['content'].startswith(prefix)
    assert "Translate each value" not in user['content'][:len(prefix)]


def test_prefix_cache_breakpoints_mark_the_shared_translation_prefix(monkeypatch):
    calls = []

    def fake_call(messages, user_id):
        calls.append(messages)
        return json.dumps({"1": "Hola"})

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    with patch('src.tasks.processing.PREFIX_CACHE_OPTIMIZED_PROMPTS', True), \
         patch('src.tasks.processing.TEXT_MODEL_CACHE_BREAKPOINTS', True):
        tt.translate_json_segments(
            json.dumps([{"speaker": "A", "sentence": "Hello"}]),
            user_id=None,
            target_language="Spanish",
        )

    content = calls[0][1]['content']
    assert isinstance(content, list) and len(content) == 2
    assert content[0]['cache_control'] == {'type': 'ephemeral'}
    assert content[1]['text'].startswith("\n\nTranslate each value")


def test_malformed_segment_response_is_not_translated_as_plain_text(monkeypatch):
    """A bad model answer for a segment chunk must fail, not be retried as a
    plain-text translation of the raw JSON string (which would persist a
    mangled transcript and defeat the job retry loop)."""
    calls = []

    def fake_call(messages, user_id):
        calls.append(messages)
        return '{"1": "only one key"}'

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    text = json.dumps([
        {"speaker": "A", "start_time": 1.0, "end_time": 2.0, "sentence": "Hello"},
        {"speaker": "B", "start_time": 2.0, "end_time": 3.0, "sentence": "World"},
    ])
    with pytest.raises(ValueError):
        tt.translate_transcription_text(text, user_id=None, target_language="Spanish")
    # The chunk is split and re-sent as segments, never as one plain-text value.
    sent = []
    for m in calls:
        c = m[-1]['content']
        sent.append(json.loads(c[c.find('{'):c.rfind('}') + 1]))
    assert all(set(p) <= {"1", "2"} for p in sent), 'the segment chunk must not be re-sent as plain text'


def test_invalid_json_array_falls_back_to_plain_text(monkeypatch):
    def fake_call(messages, user_id):
        return json.dumps({"0": "hola"})

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    out = tt.translate_transcription_text("[not valid json", user_id=None,
                                          target_language="Spanish")
    assert out == "hola"


def test_plain_text_paragraphs(monkeypatch):
    def fake_call(messages, user_id):
        content = messages[-1]['content']
        import json as _j
        start = content.find('{')
        end = content.rfind('}')
        payload = _j.loads(content[start:end + 1])
        return _j.dumps({k: (v.upper() if isinstance(v, str) else v) for k, v in payload.items()}, ensure_ascii=False)

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    text = "Para one.\n\nPara two."
    out = tt.translate_transcription_text(text, user_id=None, target_language="Spanish")
    assert "\n\n" in out
    assert "PARA ONE." in out
    assert "PARA TWO." in out


def test_chunking_no_truncation(monkeypatch):
    monkeypatch.setattr(tt, "_chunk_limit", lambda: 20)
    calls = []

    def fake_call(messages, user_id):
        content = messages[-1]['content']
        import json as _j
        start = content.find('{')
        end = content.rfind('}')
        payload = _j.loads(content[start:end + 1])
        calls.append(payload)
        return _j.dumps({k: (v + "X") for k, v in payload.items()}, ensure_ascii=False)

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    text = "A" * 15 + "\n\n" + "B" * 15 + "\n\n" + "C" * 15
    out = tt.translate_transcription_text(text, user_id=None, target_language="Spanish")
    assert len(calls) >= 2
    assert "A" * 15 in out or ("A" * 5 in out)


def test_same_language_plain_text_is_copied_without_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('the LLM must not be called for a same-language copy')

    monkeypatch.setattr(tt, '_call_llm', boom)
    text = "Hola mundo. Esto ya esta en espanol."
    out = tt.translate_transcription_text(
        text, user_id=None, target_language="Spanish", source_language="es")
    assert out == text


def test_same_language_segments_are_copied_verbatim(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('the LLM must not be called for a same-language copy')

    monkeypatch.setattr(tt, '_call_llm', boom)
    text = json.dumps([
        {"speaker": "A", "start_time": 1.0, "end_time": 2.0, "sentence": "Hola"},
        {"speaker": "B", "start_time": 2.0, "end_time": 3.0, "sentence": "Adios"},
    ])
    out = tt.translate_transcription_text(
        text, user_id=None, target_language="Spanish", source_language="es")
    assert out == text
    assert json.loads(out) == json.loads(text)


def test_language_code_and_display_name_compare_equal(monkeypatch):
    monkeypatch.setattr(tt, '_call_llm',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('no LLM')))
    assert tt.translate_transcription_text(
        "Bonjour", target_language="French", source_language="fr") == "Bonjour"
    assert tt.translate_transcription_text(
        "Hallo", target_language="de", source_language="German") == "Hallo"


def test_auto_detect_source_still_uses_llm(monkeypatch):
    calls = []

    def fake_call(messages, user_id):
        calls.append(messages)
        content = messages[-1]['content']
        import json as _j
        start = content.find('{')
        end = content.rfind('}')
        payload = _j.loads(content[start:end + 1])
        return _j.dumps({k: (v + " [es]" if isinstance(v, str) else v)
                         for k, v in payload.items()}, ensure_ascii=False)

    monkeypatch.setattr(tt, '_call_llm', fake_call)
    out = tt.translate_transcription_text(
        "Hello world", user_id=None, target_language="Spanish", source_language=None)
    assert len(calls) == 1
    assert out != "Hello world"


def test_token_usage_recorded(monkeypatch):
    username = "u_" + uuid.uuid4().hex[:14]
    email = uuid.uuid4().hex[:10] + "@example.com"
    hashed = auth_mod.bcrypt.generate_password_hash("Sup3rSecret!").decode('utf-8')
    with app.app_context():
        from src.models import User
        from src.database import db
        u = User(username=username, email=email, password=hashed)
        db.session.add(u)
        db.session.commit()
        uid = u.id

    import src.services.llm as llm
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"1":"Hola"}'))],
        usage=usage,
    )
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: completion)))
    monkeypatch.setattr(llm, 'client', fake_client)
    monkeypatch.setattr(llm, 'translation_client', fake_client)
    monkeypatch.setattr(llm, 'TEXT_MODEL_API_KEY', 'test-key')
    monkeypatch.setattr(llm, 'TEXT_MODEL_BASE_URL', 'http://localhost:11434/v1')
    monkeypatch.setattr(llm, 'TEXT_MODEL_NAME', 'gpt-4o')
    monkeypatch.setattr(llm, 'TRANSLATION_MODEL_NAME', None)

    fake_tracker = MagicMock()
    fake_tracker.check_budget.return_value = (True, 50.0, None)
    with patch('src.services.token_tracking.token_tracker', fake_tracker):
        text = json.dumps([{"speaker": "A", "sentence": "Hello"}])
        tt.translate_transcription_text(text, user_id=uid, target_language="Spanish")

    fake_tracker.record_usage.assert_called_once()
    kwargs = fake_tracker.record_usage.call_args.kwargs
    assert kwargs['operation_type'] == 'translation'
    assert kwargs['user_id'] == uid

    with app.app_context():
        from src.database import db
        from src.models import User
        u2 = db.session.get(User, uid)
        if u2:
            db.session.delete(u2)
            db.session.commit()
