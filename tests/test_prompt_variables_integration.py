"""
Integration test for prompt variable substitution in the summary task.

Exercises `generate_summary_only_task` end to end (with the LLM client
patched out) and asserts that:

1. Variables in the resolved tag/folder/user/admin prompt are substituted.
2. Variables appearing in the appended `custom_prompt_override` (issue #253
   append mode) are also substituted, since substitution must happen on the
   final composed prompt rather than on the resolved chain only.

A regression of the substitution order (substituting before append) would
produce a final prompt with literal `{{quarter}}` left in the appended
section. This test catches that.
"""

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def app_with_db():
    """Provide a Flask app context with a clean recording + tag."""
    from src.app import app, db
    from src.models import User, Recording, Tag

    with app.app_context():
        user = User.query.filter_by(username="prompt_var_test_user").first()
        created_user = False
        if not user:
            user = User(username="prompt_var_test_user", email="prompt_var_test@local.test")
            user.password = "unused"
            db.session.add(user)
            db.session.commit()
            created_user = True

        tag = Tag(
            name=f"prompt_var_test_tag_{user.id}",
            user_id=user.id,
            color="#000000",
            custom_prompt="Generate minutes for {{agenda}}.",
        )
        db.session.add(tag)
        db.session.commit()

        recording = Recording(
            user_id=user.id,
            title="Prompt Variable Test",
            original_filename="test_prompt_vars.wav",
            transcription='Hello world. This is a long enough transcription to satisfy the length check.',
            status="COMPLETED",
            prompt_variables={"agenda": "Budget review", "quarter": "Q3"},
        )
        db.session.add(recording)
        db.session.commit()

        # Apply tag to recording
        from src.models import RecordingTag
        rt = RecordingTag(recording_id=recording.id, tag_id=tag.id, order=1)
        db.session.add(rt)
        db.session.commit()

        try:
            yield app, db, user, recording, tag, created_user
        finally:
            try:
                from src.models import RecordingTag
                RecordingTag.query.filter_by(recording_id=recording.id).delete()
                db.session.delete(recording)
                db.session.delete(tag)
                if created_user:
                    db.session.delete(user)
                db.session.commit()
            except Exception:
                db.session.rollback()


def _capture_summary_prompt(app):
    """Patch `call_llm_completion` and return a holder that captures the
    user-message content of the first call. The mock returns a stub
    completion that yields a one-line summary so the task completes
    without further side-effects."""
    captured = {"user_prompt": None}

    def _fake_completion(messages=None, **kwargs):
        for msg in messages or []:
            if msg.get("role") == "user":
                captured["user_prompt"] = msg.get("content", "")
                break
        stub = MagicMock()
        stub.choices = [MagicMock()]
        stub.choices[0].message.content = "Test summary."
        stub.usage = MagicMock()
        stub.usage.prompt_tokens = 1
        stub.usage.completion_tokens = 1
        stub.usage.total_tokens = 2
        return stub

    return captured, _fake_completion


def test_variables_substitute_in_resolved_prompt(app_with_db):
    """A `{{name}}` in a tag's custom_prompt is replaced with the recording's stored value."""
    app, db, user, recording, tag, created_user = app_with_db
    from src.tasks.processing import generate_summary_only_task

    captured, fake_completion = _capture_summary_prompt(app)
    with patch("src.tasks.processing.call_llm_completion", side_effect=fake_completion), \
         patch("src.tasks.processing.client", new=MagicMock()):
        generate_summary_only_task(app.app_context(), recording.id, user_id=user.id)

    prompt = captured["user_prompt"] or ""
    assert "Budget review" in prompt, f"agenda value missing from prompt: {prompt[:500]}"
    assert "{{agenda}}" not in prompt, f"placeholder leaked into prompt: {prompt[:500]}"


def test_variables_substitute_in_appended_custom_prompt(app_with_db):
    """A `{{name}}` in `custom_prompt_override` (append mode) is also substituted.

    This is the regression test for the order-of-operations bug where the
    substitution step ran before the append step, leaving placeholders in
    the appended text intact.
    """
    app, db, user, recording, tag, created_user = app_with_db
    from src.tasks.processing import generate_summary_only_task

    captured, fake_completion = _capture_summary_prompt(app)
    with patch("src.tasks.processing.call_llm_completion", side_effect=fake_completion), \
         patch("src.tasks.processing.client", new=MagicMock()):
        generate_summary_only_task(
            app.app_context(),
            recording.id,
            custom_prompt_override="Also discuss strategic priorities for {{quarter}}.",
            custom_prompt_append=True,
            user_id=user.id,
        )

    prompt = captured["user_prompt"] or ""
    # Both the resolved-chain variable and the append-text variable must substitute.
    assert "Budget review" in prompt, f"agenda value missing: {prompt[:500]}"
    assert "Q3" in prompt, f"quarter value (from append text) missing: {prompt[:500]}"
    assert "{{agenda}}" not in prompt, f"agenda placeholder leaked: {prompt[:500]}"
    assert "{{quarter}}" not in prompt, f"quarter placeholder leaked from append text: {prompt[:500]}"
