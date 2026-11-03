"""Prompt construction for transcription title and summary generation.

The builders here are pure: callers pass the transcript text, the resolved
instructions and the output language, and get back the system message and the
user prompt. The opt-in prefix-cache layout is a parameter, not module state, so
the processing module keeps the environment flags and tests can toggle them.
"""

import json
import re

# Must be byte-identical between the title and summary calls. Keep it short and
# generic; per-task guidance belongs in the user-message suffix.
SHARED_LLM_SYSTEM_MSG = (
    "You assist with meeting transcripts. Follow the user's specific "
    "instruction precisely and output only what is requested."
)

# Backwards-compatible private name for callers that imported it from
# src.tasks.processing.
_SHARED_LLM_SYSTEM_MSG = SHARED_LLM_SYSTEM_MSG


def shared_user_prefix(transcript_text):
    """Byte-identical user-message prefix shared by the title and summary calls.

    DO NOT insert any conditional content (language, date, context, user info)
    before the closing triple-quote. Any byte that differs between the two calls
    past the first divergence kills KV-cache reuse for everything that follows --
    including the transcript itself, which is the expensive part.
    """
    return f'Transcript:\n"""\n{transcript_text}\n"""\n\n'


# Backwards-compatible private name.
_shared_user_prefix = shared_user_prefix


def build_user_content(prompt_text, transcript_text, prefix_cache_optimized, cache_breakpoints):
    """User-message content for a prompt that may start with the shared prefix.

    A plain string, except with prefix-cache prompts and text-model cache
    breakpoints both on: then two text parts, the shared prefix marked with
    cache_control and the rest of the prompt (#488).
    """
    if not (prefix_cache_optimized and cache_breakpoints):
        return prompt_text
    prefix = shared_user_prefix(transcript_text)
    if not prompt_text.startswith(prefix):
        return prompt_text
    return [
        {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": prompt_text[len(prefix):]},
    ]


def format_transcription_for_llm(transcription_text, include_timestamps=False, template_format=None):
    """Formats transcription for LLM. If it's our simplified JSON, convert it to plain text.
    Otherwise, return as is.

    When include_timestamps is True the transcript is rendered with a transcript
    template (the given template_format, or a built-in default timestamp format)
    so the summarizer / chat can reference times (#304).
    """
    if include_timestamps:
        from src.utils.transcript_render import render_transcription
        rendered = render_transcription(transcription_text, template_format)
        if rendered is not None:
            return rendered
    try:
        transcription_data = json.loads(transcription_text)
        if isinstance(transcription_data, list):
            # It's our simplified JSON format
            formatted_lines = []
            for segment in transcription_data:
                speaker = segment.get('speaker', 'Unknown Speaker')
                sentence = segment.get('sentence', '')
                formatted_lines.append(f"[{speaker}]: {sentence}")
            return "\n".join(formatted_lines)
    except (json.JSONDecodeError, TypeError):
        # Not a JSON, or not the format we expect, so return as is.
        pass
    return transcription_text


def clean_llm_response(text):
    """Clean LLM responses by removing thinking tags and excessive whitespace.

    This handles responses from reasoning models that include <think> tags.
    """
    if not text:
        return ""

    # Remove thinking tags and their content
    # Handle both <think> and <thinking> tags with various closing formats
    cleaned = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Also handle unclosed thinking tags (in case the model doesn't close them)
    cleaned = re.sub(r'<think(?:ing)?>.*$', '', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Remove any remaining XML-like tags that might be related to thinking
    # but preserve markdown formatting
    cleaned = re.sub(r'<(?!/?(?:code|pre|blockquote|p|br|hr|ul|ol|li|h[1-6]|em|strong|b|i|a|img)(?:\s|>|/))[^>]+>', '', cleaned)

    # Clean up excessive whitespace while preserving intentional formatting
    # Handle lines individually to preserve Markdown hard line breaks (two spaces at end)
    lines = cleaned.split('\n')
    cleaned_lines = []

    for line in lines:
        # If a line consists only of whitespace (e.g., after tag removal),
        # make it completely empty. This is necessary for the \n{3,} regex to work later.
        if not line.strip():
            cleaned_lines.append("")
        else:
            # Preserve lines containing text exactly as they are
            # This keeps trailing spaces needed for Markdown hard line breaks intact
            cleaned_lines.append(line)

    # Join lines and collapse 3+ consecutive newlines into exactly 2 (one blank line)
    cleaned = '\n'.join(cleaned_lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

    # Final strip to remove leading/trailing whitespace
    return cleaned.strip()


def build_title_prompt(transcript_text, title_instructions, user_output_language, prefix_cache_optimized):
    """Return (system_message_content, prompt_text) for AI title generation."""
    language_directive = f"Please provide the title in {user_output_language}." if user_output_language else ""

    if prefix_cache_optimized:
        # Shared-prefix layout: identical system message + identical user prefix
        # (including the transcript) between this call and the summary call, so
        # a prefix-caching backend can reuse the transcript KV cache. Everything
        # task-specific goes in the SUFFIX after the transcript block. This
        # prefix MUST stay byte-identical to the one in build_summary_prompt.
        system_message_content = SHARED_LLM_SYSTEM_MSG
        prompt_text = (
            shared_user_prefix(transcript_text)
            + "Task: produce ONE title for the conversation above.\n"
            + "Requirements:\n"
            + title_instructions + "\n"
            + "- Output ONLY the title text, nothing else (no quotes, no prefix)\n"
            + (f"- Respond in {user_output_language}\n" if user_output_language else "")
            + "\nTitle:"
        )
    else:
        prompt_text = f"""Create a short title for this conversation:

{transcript_text}

Requirements:
{title_instructions}
- Output ONLY the title text, nothing else

{language_directive}

Title:"""

        system_message_content = "You are an AI assistant that generates concise titles for audio transcriptions. Respond only with the title."
        if user_output_language:
            system_message_content += f" Ensure your response is in {user_output_language}."

    return system_message_content, prompt_text


def build_incognito_summary_prompt(transcript_text, summarization_instructions,
                                   user_output_language):
    """Return (system_message_content, prompt_text) for an incognito summary.

    Incognito recordings have no stored context, so this is the summary layout
    without the context section and without the prefix-cache variants.
    """
    language_directive = (
        f"IMPORTANT: You MUST provide the summary in {user_output_language}."
        if user_output_language else ""
    )

    system_message_content = (
        "You are an AI assistant that generates comprehensive summaries for "
        "meeting transcripts. Respond only with the summary in Markdown format."
    )
    if user_output_language:
        system_message_content += (
            f" You MUST generate the entire summary in {user_output_language}."
        )

    prompt_text = f"""Transcription:
\"\"\"
{transcript_text}
\"\"\"

Summarization Instructions:
{summarization_instructions}

{language_directive}"""

    return system_message_content, prompt_text


def build_summary_prompt(transcript_text, context_section, summarization_instructions,
                         user_output_language, prefix_cache_optimized):
    """Return (system_message_content, prompt_text) for summary generation."""
    language_directive = f"IMPORTANT: You MUST provide the summary in {user_output_language}. The entire response must be in {user_output_language}." if user_output_language else ""

    if prefix_cache_optimized:
        # Shared-prefix layout: the system message and the user-message bytes
        # up to and including the transcript block MUST match build_title_prompt
        # byte-for-byte, so the transcript KV cache built by the title call is
        # reusable here. All variability (role guidance, context, instructions,
        # language) goes in the SUFFIX after the transcript block.
        system_message_content = SHARED_LLM_SYSTEM_MSG
        suffix_parts = [
            (
                "Task: produce a comprehensive Markdown summary of the "
                "transcript above. Output raw Markdown only -- no code fences, "
                "no preamble, no commentary."
            ),
            "",
            context_section,
            "",
            "Summarization Instructions:",
            summarization_instructions,
        ]
        if user_output_language:
            suffix_parts.append("")
            suffix_parts.append(
                f"Language Requirement: You MUST write the entire summary "
                f"in {user_output_language}. This is mandatory."
            )
        prompt_text = shared_user_prefix(transcript_text) + "\n".join(suffix_parts)
    else:
        # Build SYSTEM message: Initial instructions + Context + Language
        system_message_content = "You are an AI assistant that generates comprehensive summaries for meeting transcripts. Respond only with the summary in Markdown format. Do NOT use markdown code blocks (```markdown). Provide raw markdown content directly."
        system_message_content += f"\n\n{context_section}"
        if user_output_language:
            system_message_content += f"\n\nLanguage Requirement: You MUST generate the entire summary in {user_output_language}. This is mandatory."

        # Build USER message: Transcription + Summarization Instructions + Language Directive
        prompt_text = f"""Transcription:
\"\"\"
{transcript_text}
\"\"\"

Summarization Instructions:
{summarization_instructions}

{language_directive}"""

    return system_message_content, prompt_text
