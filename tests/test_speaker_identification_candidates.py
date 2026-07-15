"""Candidate-constrained contextual speaker identification tests."""

from types import SimpleNamespace
import json
from unittest.mock import Mock, patch

from flask import Flask

from src.services.speaker_identification import (
    apply_contextual_auto_labels,
    identify_speakers_from_transcript,
)
from src.tasks.processing import _replace_speaker_embeddings


TRANSCRIPT = [
    {"speaker": "S01", "sentence": "Maha, can you review this?"},
    {"speaker": "S02", "sentence": "Yes, I will send it today."},
]


def _completion(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_candidate_names_are_added_to_prompt_and_enforced(monkeypatch):
    monkeypatch.delenv("AUTO_IDENTIFY_RESPONSE_SCHEMA", raising=False)
    app = Flask(__name__)
    with app.app_context(), patch(
        "src.services.llm.call_llm_completion",
        return_value=_completion('{"SPEAKER_00":"Maha","SPEAKER_01":"Jill"}'),
    ) as call_llm, patch(
        "src.models.SystemSetting.get_setting", return_value=30000
    ):
        result = identify_speakers_from_transcript(
            TRANSCRIPT,
            user_id=1,
            candidate_names=["Jael", "Maha", "Siva"],
        )

    assert result == {"S01": "Maha", "S02": ""}
    prompt = call_llm.call_args.kwargs["messages"][1]["content"]
    assert "Known speaker profiles: Jael, Maha, Siva" in prompt
    assert "Use only these exact names" in prompt


def test_punctuated_saved_name_is_preserved(monkeypatch):
    monkeypatch.delenv("AUTO_IDENTIFY_RESPONSE_SCHEMA", raising=False)
    app = Flask(__name__)
    with app.app_context(), patch(
        "src.services.llm.call_llm_completion",
        return_value=_completion('{"SPEAKER_00":"Smith, John","SPEAKER_01":""}'),
    ), patch("src.models.SystemSetting.get_setting", return_value=30000):
        result = identify_speakers_from_transcript(
            TRANSCRIPT, user_id=1, candidate_names=["Smith, John"]
        )

    assert result == {"S01": "Smith, John", "S02": ""}


def test_manual_identification_remains_unconstrained(monkeypatch):
    monkeypatch.delenv("AUTO_IDENTIFY_RESPONSE_SCHEMA", raising=False)
    app = Flask(__name__)
    with app.app_context(), patch(
        "src.services.llm.call_llm_completion",
        return_value=_completion('{"SPEAKER_00":"Maha","SPEAKER_01":"Jill"}'),
    ), patch("src.models.SystemSetting.get_setting", return_value=30000):
        result = identify_speakers_from_transcript(TRANSCRIPT, user_id=1)

    assert result == {"S01": "Maha", "S02": "Jill"}


def test_reprocessing_clears_stale_speaker_embeddings():
    recording = SimpleNamespace(speaker_embeddings={"S01": [0.1, 0.2]})
    response = SimpleNamespace(speaker_embeddings=None)

    assert _replace_speaker_embeddings(recording, response) is None
    assert recording.speaker_embeddings is None

    response.speaker_embeddings = {"S02": [0.3, 0.4]}
    assert _replace_speaker_embeddings(recording, response) == {"S02": [0.3, 0.4]}
    assert recording.speaker_embeddings == {"S02": [0.3, 0.4]}


def _speaker_model(names):
    query = Mock()
    query.filter_by.return_value.order_by.return_value.all.return_value = [
        SimpleNamespace(name=name) for name in names
    ]
    return SimpleNamespace(
        query=query,
        name=SimpleNamespace(asc=lambda: "speaker-name"),
    )


def test_contextual_auto_labelling_is_opt_in():
    recording = SimpleNamespace(id=9, transcription=json.dumps(TRANSCRIPT))
    user = SimpleNamespace(id=1, auto_speaker_labelling=False)
    with patch(
        "src.services.speaker_identification.identify_speakers_from_transcript"
    ) as identify:
        assert apply_contextual_auto_labels(recording, user) == {}
    identify.assert_not_called()


def test_contextual_auto_labelling_skips_users_without_profiles():
    recording = SimpleNamespace(id=9, transcription=json.dumps(TRANSCRIPT))
    user = SimpleNamespace(id=1, auto_speaker_labelling=True)
    with Flask(__name__).app_context(), patch(
        "src.models.Speaker", _speaker_model([])
    ), patch(
        "src.services.speaker_identification.identify_speakers_from_transcript"
    ) as identify:
        assert apply_contextual_auto_labels(recording, user) == {}
    identify.assert_not_called()


def test_contextual_auto_labelling_applies_only_saved_matches():
    recording = SimpleNamespace(id=9, transcription=json.dumps(TRANSCRIPT))
    user = SimpleNamespace(id=1, auto_speaker_labelling=True)
    with Flask(__name__).app_context(), patch(
        "src.models.Speaker", _speaker_model(["Jael", "Maha", "Siva"])
    ), patch(
        "src.services.speaker_identification.identify_speakers_from_transcript",
        return_value={"S01": "Maha", "S02": ""},
    ) as identify, patch(
        "src.services.speaker_embedding_matcher.apply_speaker_names_to_transcription",
        return_value=True,
    ) as apply_names, patch(
        "src.services.speaker_snippets.create_speaker_snippets"
    ) as create_snippets:
        result = apply_contextual_auto_labels(recording, user)

    assert result == {"S01": "Maha"}
    assert identify.call_args.kwargs["candidate_names"] == ["Jael", "Maha", "Siva"]
    apply_names.assert_called_once_with(recording, {"S01": "Maha"})
    create_snippets.assert_called_once_with(
        9, {"S01": {"name": "Maha", "isMe": False}}
    )


def test_contextual_auto_labelling_failure_is_isolated():
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
