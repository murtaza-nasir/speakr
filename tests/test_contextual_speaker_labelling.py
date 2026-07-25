"""Contextual (embedding-less) speaker auto-labelling.

Covers the candidate-constrained LLM path added for connectors that diarize but
return no voice embeddings: exact-name constraint, the prefix-cache-friendly
transcript-first layout, the admin-editable guidance suffix, and the opt-in
apply_contextual_auto_labels orchestration. The manual identify path (no
candidates) must remain unconstrained.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.speaker_identification import (
    identify_speakers_from_transcript,
    apply_contextual_auto_labels,
    _sanitize_identified_map,
)
from src.config.prompts import DEFAULT_CONTEXTUAL_SPEAKER_PROMPT


TRANSCRIPT = [
    {"speaker": "S01", "sentence": "Maha, can you review this?"},
    {"speaker": "S02", "sentence": "Yes, I will send it today."},
]


def _completion(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _settings(contextual_prompt=None, transcript_limit=30000):
    """Side effect for SystemSetting.get_setting keyed by setting name."""
    def _get(key, default=None):
        if key == 'transcript_length_limit':
            return transcript_limit
        if key == 'admin_default_contextual_speaker_prompt':
            return contextual_prompt
        return default
    return _get


def _speaker_model(names):
    query = Mock()
    query.filter_by.return_value.order_by.return_value.all.return_value = [
        SimpleNamespace(name=name) for name in names
    ]
    return SimpleNamespace(query=query, name=SimpleNamespace(asc=lambda: "speaker-name"))


# --- sanitizer constraint ---

def test_sanitizer_constrains_to_saved_names_and_preserves_punctuation():
    app = Flask(__name__)
    with app.app_context():
        out = _sanitize_identified_map(
            {"SPEAKER_00": "maha", "SPEAKER_01": "Nobody", "SPEAKER_02": "Smith, John"},
            ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
            candidate_names=["Maha", "Smith, John"],
        )
    assert out == {"SPEAKER_00": "Maha", "SPEAKER_01": "", "SPEAKER_02": "Smith, John"}


# --- prompt / constraint behaviour ---

def test_candidates_constrain_output_and_appear_in_prompt(monkeypatch):
    monkeypatch.delenv("AUTO_IDENTIFY_RESPONSE_SCHEMA", raising=False)
    app = Flask(__name__)
    with app.app_context(), patch(
        "src.services.llm.call_llm_completion",
        return_value=_completion('{"SPEAKER_00":"Maha","SPEAKER_01":"Jill"}'),
    ) as call_llm, patch("src.models.SystemSetting.get_setting", side_effect=_settings()):
        result = identify_speakers_from_transcript(
            TRANSCRIPT, user_id=1, candidate_names=["Jael", "Maha", "Siva"]
        )
    # "Jill" is not a saved profile, so it is dropped.
    assert result == {"S01": "Maha", "S02": ""}
    prompt = call_llm.call_args.kwargs["messages"][1]["content"]
    assert "Known speaker profiles you may assign: Jael, Maha, Siva" in prompt
    assert DEFAULT_CONTEXTUAL_SPEAKER_PROMPT in prompt


def test_manual_path_is_unconstrained(monkeypatch):
    monkeypatch.delenv("AUTO_IDENTIFY_RESPONSE_SCHEMA", raising=False)
    app = Flask(__name__)
    with app.app_context(), patch(
        "src.services.llm.call_llm_completion",
        return_value=_completion('{"SPEAKER_00":"Maha","SPEAKER_01":"Jill"}'),
    ), patch("src.models.SystemSetting.get_setting", side_effect=_settings()):
        result = identify_speakers_from_transcript(TRANSCRIPT, user_id=1)
    assert result == {"S01": "Maha", "S02": "Jill"}


def test_admin_prompt_override_replaces_default_guidance(monkeypatch):
    monkeypatch.delenv("AUTO_IDENTIFY_RESPONSE_SCHEMA", raising=False)
    app = Flask(__name__)
    custom = "CUSTOM-ADMIN-GUIDANCE-XYZ do your best."
    with app.app_context(), patch(
        "src.services.llm.call_llm_completion",
        return_value=_completion('{"SPEAKER_00":""}'),
    ) as call_llm, patch(
        "src.models.SystemSetting.get_setting", side_effect=_settings(contextual_prompt=custom)
    ):
        identify_speakers_from_transcript(TRANSCRIPT, user_id=1, candidate_names=["Maha"])
    prompt = call_llm.call_args.kwargs["messages"][1]["content"]
    assert custom in prompt
    assert DEFAULT_CONTEXTUAL_SPEAKER_PROMPT not in prompt


def test_prefix_cache_layout_puts_transcript_first_and_guidance_last(monkeypatch):
    monkeypatch.delenv("AUTO_IDENTIFY_RESPONSE_SCHEMA", raising=False)
    from src.tasks.processing import _SHARED_LLM_SYSTEM_MSG
    app = Flask(__name__)
    with app.app_context(), patch(
        "src.tasks.processing.PREFIX_CACHE_OPTIMIZED_PROMPTS", True
    ), patch(
        "src.services.llm.call_llm_completion",
        return_value=_completion('{"SPEAKER_00":"Maha"}'),
    ) as call_llm, patch("src.models.SystemSetting.get_setting", side_effect=_settings()):
        identify_speakers_from_transcript(TRANSCRIPT, user_id=1, candidate_names=["Maha"])
    messages = call_llm.call_args.kwargs["messages"]
    # System message matches the shared one used by title/summary.
    assert messages[0]["content"] == _SHARED_LLM_SYSTEM_MSG
    user_msg = messages[1]["content"]
    # Transcript-first shared prefix, guidance suffix strictly after it.
    assert user_msg.startswith('Transcript:\n"""\n')
    assert user_msg.index("Known speaker profiles") > user_msg.index('"""')


# --- apply_contextual_auto_labels orchestration ---

def test_apply_is_opt_in():
    recording = SimpleNamespace(id=9, transcription=json.dumps(TRANSCRIPT))
    user = SimpleNamespace(id=1, auto_speaker_labelling=False)
    with patch(
        "src.services.speaker_identification.identify_speakers_from_transcript"
    ) as ident:
        assert apply_contextual_auto_labels(recording, user) == {}
    ident.assert_not_called()


def test_apply_skips_users_without_profiles():
    recording = SimpleNamespace(id=9, transcription=json.dumps(TRANSCRIPT))
    user = SimpleNamespace(id=1, auto_speaker_labelling=True)
    with Flask(__name__).app_context(), patch(
        "src.models.Speaker", _speaker_model([])
    ), patch(
        "src.services.speaker_identification.identify_speakers_from_transcript"
    ) as ident:
        assert apply_contextual_auto_labels(recording, user) == {}
    ident.assert_not_called()


def test_apply_applies_only_saved_matches_and_creates_snippets():
    recording = SimpleNamespace(id=9, transcription=json.dumps(TRANSCRIPT))
    user = SimpleNamespace(id=1, auto_speaker_labelling=True)
    with Flask(__name__).app_context(), patch(
        "src.models.Speaker", _speaker_model(["Jael", "Maha", "Siva"])
    ), patch(
        "src.services.speaker_identification.identify_speakers_from_transcript",
        return_value={"S01": "Maha", "S02": ""},
    ) as ident, patch(
        "src.services.speaker_embedding_matcher.apply_speaker_names_to_transcription",
        return_value=True,
    ) as apply_names, patch(
        "src.services.speaker_snippets.create_speaker_snippets"
    ) as snippets:
        result = apply_contextual_auto_labels(recording, user)
    assert result == {"S01": "Maha"}
    assert ident.call_args.kwargs["candidate_names"] == ["Jael", "Maha", "Siva"]
    apply_names.assert_called_once_with(recording, {"S01": "Maha"})
    snippets.assert_called_once_with(9, {"S01": {"name": "Maha", "isMe": False}})


def test_apply_failure_is_isolated():
    recording = SimpleNamespace(id=9, transcription=json.dumps(TRANSCRIPT))
    user = SimpleNamespace(id=1, auto_speaker_labelling=True)
    with Flask(__name__).app_context(), patch(
        "src.models.Speaker", _speaker_model(["Maha"])
    ), patch(
        "src.services.speaker_identification.identify_speakers_from_transcript",
        side_effect=RuntimeError("LLM unavailable"),
    ), patch(
        "src.services.speaker_embedding_matcher.apply_speaker_names_to_transcription"
    ) as apply_names:
        assert apply_contextual_auto_labels(recording, user) == {}
    apply_names.assert_not_called()
