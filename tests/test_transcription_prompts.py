"""The prompt module owns the title/summary prompt contract.

These are pure tests: they pin the two builders and the shared prefix without
Flask, a database or an LLM. The end-to-end prefix-cache behaviour (that
processing.py wires the flag and the cache breakpoints correctly) stays covered
by tests/test_prefix_cache_prompts.py.
"""

import src.transcription_prompts as prompts

TRANSCRIPT = "Alice: Let's lock the launch date.\nBob: The 14th works."


def _shared_region(user_prompt):
    """Substring up to and including the closing transcript fence."""
    marker = '"""\n\n'
    idx = user_prompt.find(marker)
    assert idx != -1, "prompt does not contain the shared transcript block"
    return user_prompt[: idx + len(marker)]


def test_title_and_summary_share_system_message_and_prefix():
    title_system, title_user = prompts.build_title_prompt(
        TRANSCRIPT, "Be specific.", "Spanish", True
    )
    summary_system, summary_user = prompts.build_summary_prompt(
        TRANSCRIPT, "Context:\n- none", "Summarize.", "Spanish", True
    )

    assert title_system == summary_system == prompts.SHARED_LLM_SYSTEM_MSG
    assert _shared_region(title_user) == _shared_region(summary_user)
    assert _shared_region(title_user) == prompts.shared_user_prefix(TRANSCRIPT)


def test_task_specific_content_stays_after_the_transcript_block():
    _, title_user = prompts.build_title_prompt(
        TRANSCRIPT, "Be specific.", "Spanish", True
    )
    _, summary_user = prompts.build_summary_prompt(
        TRANSCRIPT, "Context:\n- context-only-token", "Summarize.", "Spanish", True
    )
    prefix_len = len(prompts.shared_user_prefix(TRANSCRIPT))

    assert "Spanish" not in title_user[:prefix_len]
    assert "context-only-token" not in summary_user[:prefix_len]
    assert "Be specific." in title_user[prefix_len:]
    assert "context-only-token" in summary_user[prefix_len:]


def test_flag_off_keeps_the_upstream_layout():
    title_system, title_user = prompts.build_title_prompt(
        TRANSCRIPT, "Be specific.", None, False
    )
    summary_system, summary_user = prompts.build_summary_prompt(
        TRANSCRIPT, "Context:\n- none", "Summarize.", None, False
    )

    assert title_user.startswith("Create a short title for this conversation:")
    assert summary_user.startswith("Transcription:")
    assert title_system != summary_system


def test_build_user_content_marks_only_the_shared_prefix():
    _, user = prompts.build_title_prompt(TRANSCRIPT, "Be specific.", None, True)

    parts = prompts.build_user_content(user, TRANSCRIPT, True, True)

    assert isinstance(parts, list)
    assert len(parts) == 2
    assert parts[0]["cache_control"] == {"type": "ephemeral"}
    assert parts[0]["text"] == prompts.shared_user_prefix(TRANSCRIPT)
    assert "".join(part["text"] for part in parts) == user


def test_build_user_content_is_a_noop_without_both_flags():
    _, user = prompts.build_title_prompt(TRANSCRIPT, "Be specific.", None, True)

    assert prompts.build_user_content(user, TRANSCRIPT, False, True) is user
    assert prompts.build_user_content(user, TRANSCRIPT, True, False) is user


def test_incognito_summary_keeps_the_upstream_layout():
    system_message, user_message = prompts.build_incognito_summary_prompt(
        TRANSCRIPT, "Summarize.", "French"
    )

    assert "Respond only with the summary in Markdown format." in system_message
    assert "entire summary in French" in system_message
    assert user_message.startswith("Transcription:\n\"\"\"\n")
    assert "Summarization Instructions:\nSummarize." in user_message
    assert user_message.rstrip().endswith(
        "IMPORTANT: You MUST provide the summary in French."
    )

