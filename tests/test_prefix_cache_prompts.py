"""
Tests for the opt-in prefix-cache-friendly prompt layout
(PREFIX_CACHE_OPTIMIZED_PROMPTS).

The title-generation and summary-generation calls run over the same transcript
back-to-back. When the flag is ON, both calls must use a byte-identical system
message AND a byte-identical user-message prefix up to and including the
transcript block, so a prefix-caching inference backend can reuse the transcript
KV cache across the two calls. Everything task-specific must live in the suffix
AFTER the transcript.

When the flag is OFF, the prompts must be exactly the upstream prompts (no
behavioural change). These tests assert both guarantees.
"""

import pytest
from unittest.mock import patch, MagicMock

import src.tasks.processing as processing


# Marker that ends the byte-identical shared region. _shared_user_prefix wraps
# the transcript as: Transcript:\n"""\n{transcript}\n"""\n\n  — so everything up
# to and including the closing """ + blank line must match between the calls.
def _shared_region(user_prompt):
    """Return the substring of a user prompt up to and including the closing
    transcript fence produced by _shared_user_prefix, or None if not present."""
    marker = '"""\n\n'
    idx = user_prompt.find(marker)
    if idx == -1:
        return None
    return user_prompt[: idx + len(marker)]


@pytest.fixture
def app_with_recording():
    """A Flask app context with a single recording owned by a test user."""
    from src.app import app, db
    from src.models import User, Recording

    with app.app_context():
        user = User.query.filter_by(username="prefix_cache_test_user").first()
        created_user = False
        if not user:
            user = User(username="prefix_cache_test_user", email="prefix_cache_test@local.test")
            user.password = "unused"
            db.session.add(user)
            db.session.commit()
            created_user = True

        recording = Recording(
            user_id=user.id,
            title="Prefix Cache Test",
            original_filename="test_prefix_cache.wav",
            transcription=(
                "Alice: Let's lock the launch date. "
                "Bob: I propose the 14th. "
                "Alice: Agreed, the 14th it is. This is long enough to summarise."
            ),
            status="COMPLETED",
        )
        db.session.add(recording)
        db.session.commit()

        try:
            yield app, db, user, recording, created_user
        finally:
            try:
                db.session.delete(recording)
                if created_user:
                    db.session.delete(user)
                db.session.commit()
            except Exception:
                db.session.rollback()


def _capture_messages():
    """Patch target for call_llm_completion: records the messages of every call
    and returns a stub completion so the caller proceeds normally."""
    calls = []

    def _fake_completion(messages=None, **kwargs):
        calls.append(messages or [])
        stub = MagicMock()
        stub.choices = [MagicMock()]
        # A short, single-line response works for both title and summary.
        stub.choices[0].message.content = "Launch date locked"
        stub.choices[0].message.reasoning = None
        stub.usage = MagicMock()
        stub.usage.prompt_tokens = 1
        stub.usage.completion_tokens = 1
        stub.usage.total_tokens = 2
        return stub

    return calls, _fake_completion


def _run_both_calls(app, recording, user):
    """Run the title call then the summary call, returning (title_messages,
    summary_messages) as {role: content} dicts."""
    calls, fake_completion = _capture_messages()
    with patch("src.tasks.processing.call_llm_completion", side_effect=fake_completion), \
         patch("src.tasks.processing.client", new=MagicMock()):
        processing._generate_ai_title(recording)
        title_messages = dict((m["role"], m["content"]) for m in calls[-1])

        calls.clear()
        processing.generate_summary_only_task(app.app_context(), recording.id, user_id=user.id)
        # The first captured call in the summary task is the summary itself.
        summary_messages = dict((m["role"], m["content"]) for m in calls[0])

    return title_messages, summary_messages


def test_flag_on_shares_system_and_transcript_prefix(app_with_recording):
    """With the flag ON, title and summary share a byte-identical system message
    and a byte-identical user-message prefix through the transcript block."""
    app, db, user, recording, created_user = app_with_recording

    with patch.object(processing, "PREFIX_CACHE_OPTIMIZED_PROMPTS", True):
        title_msgs, summary_msgs = _run_both_calls(app, recording, user)

    # System message identical.
    assert title_msgs["system"] == summary_msgs["system"], (
        "system messages diverge with the flag ON:\n"
        f"title:   {title_msgs['system']!r}\n"
        f"summary: {summary_msgs['system']!r}"
    )

    # Shared transcript-first prefix present and identical.
    title_prefix = _shared_region(title_msgs["user"])
    summary_prefix = _shared_region(summary_msgs["user"])
    assert title_prefix is not None, "transcript fence missing from title user prompt"
    assert summary_prefix is not None, "transcript fence missing from summary user prompt"
    assert title_prefix == summary_prefix, (
        "user-message prefix (through the transcript block) diverges with flag ON:\n"
        f"title:   {title_prefix!r}\n"
        f"summary: {summary_prefix!r}"
    )

    # The transcript must actually be inside the shared region (sanity).
    assert "This is long enough to summarise." in title_prefix
    # Per-task wording must be in the SUFFIX, not the shared prefix.
    assert "Title:" not in title_prefix
    assert "Summarization Instructions:" not in summary_prefix


def test_flag_on_with_summary_timestamps_keeps_shared_prefix(app_with_recording):
    """Flag ON + summary timestamps ON (#304): the title must mirror the
    summary's TIMESTAMPED transcript, otherwise the shared prefix diverges and
    the KV cache can't be reused. The shared prefix must stay byte-identical and
    actually contain timestamps."""
    import json
    app, db, user, recording, created_user = app_with_recording
    # JSON-segment transcript so timestamps are actually applied.
    recording.transcription = json.dumps([
        {"speaker": "Alice", "sentence": "Let's lock the launch date.", "start_time": 0.0, "end_time": 3.0},
        {"speaker": "Bob", "sentence": "I propose the 14th, this is long enough.", "start_time": 65.0, "end_time": 68.0},
    ])
    user.summary_include_timestamps = True
    user.summary_timestamp_template_id = None
    db.session.commit()
    try:
        with patch.object(processing, "PREFIX_CACHE_OPTIMIZED_PROMPTS", True):
            title_msgs, summary_msgs = _run_both_calls(app, recording, user)

        title_prefix = _shared_region(title_msgs["user"])
        summary_prefix = _shared_region(summary_msgs["user"])
        assert title_prefix is not None and summary_prefix is not None
        assert title_prefix == summary_prefix, (
            "title/summary transcript prefix diverges when summary timestamps are on:\n"
            f"title:   {title_prefix!r}\n"
            f"summary: {summary_prefix!r}"
        )
        # The shared transcript carries timestamps (proving the title mirrored
        # the summary's timestamped format, not the plain one).
        assert "[00:00:00]" in title_prefix
        assert "[00:01:05]" in title_prefix
    finally:
        user.summary_include_timestamps = False
        db.session.commit()


def test_flag_off_keeps_upstream_prompts(app_with_recording):
    """With the flag OFF, the prompts are the original upstream prompts: distinct
    system messages and the original transcript wrappers."""
    app, db, user, recording, created_user = app_with_recording

    with patch.object(processing, "PREFIX_CACHE_OPTIMIZED_PROMPTS", False):
        title_msgs, summary_msgs = _run_both_calls(app, recording, user)

    # Upstream system messages are distinct and use the original wording.
    assert title_msgs["system"] == (
        "You are an AI assistant that generates concise titles for audio "
        "transcriptions. Respond only with the title."
    )
    assert summary_msgs["system"].startswith(
        "You are an AI assistant that generates comprehensive summaries for "
        "meeting transcripts."
    )
    assert title_msgs["system"] != summary_msgs["system"]

    # Upstream user-message wrappers.
    assert title_msgs["user"].startswith("Create a short title for this conversation:")
    assert summary_msgs["user"].startswith('Transcription:\n"""')
    assert "Summarization Instructions:" in summary_msgs["user"]

    # And they do NOT share the transcript-first prefix.
    assert _shared_region(title_msgs["user"]) is None
