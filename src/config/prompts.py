"""Default prompt templates — single source of truth.

Kept dependency-free (no app/model imports) so both ``src/init_db.py`` (which
seeds the admin setting on a fresh database) and ``src/tasks/processing.py``
(which uses it as a runtime fallback) can import it cheaply without circular
imports. Previously this text was copy-pasted in three places and could drift;
change it here only.
"""

# The summary prompt a fresh install ships with, and the fallback used at
# summarization time when no per-recording / tag / folder / user / admin prompt
# is set. To change the shipped default, edit this string.
DEFAULT_SUMMARY_PROMPT = """Identify the key issues discussed. First, give me minutes. Then, give me the key issues discussed. Then, any key takeaways. Then, any next steps (with responsible party for each step). Then, all important things that I didn't ask for but that need to be recorded. Make sure every important nuance is covered.

Example Format:

### Minutes

**Meeting Participants:**
- Bob
- Alice

---

**1. Introduction and Overview:**
- Alice expressed interest in understanding the responsibilities at the north division and the potential for technological innovations.
....

### Key Issues Discussed
....

//and so on and so forth. Make sure not to miss any nuance or details."""


# The admin-editable instruction body for CONTEXTUAL speaker labelling — used
# when a recording has no voice embeddings and the app falls back to naming
# speakers from the transcript, constrained to the user's saved speaker
# profiles. This is only the guidance paragraph; the code appends the concrete
# candidate list, the speaker labels, and the strict JSON output contract after
# it, so those cannot be broken by an edit here. It is deliberately the trailing
# (variable) part of a transcript-first prompt so it stays prefix-cache friendly
# (see PREFIX_CACHE_OPTIMIZED_PROMPTS): the transcript is the stable prefix, this
# guidance is the editable suffix.
DEFAULT_CONTEXTUAL_SPEAKER_PROMPT = """Work out which of the known speaker profiles each speaker label most likely corresponds to, using clues in the conversation such as the names people are addressed by, self-introductions, and references to roles or relationships. Only assign a known profile when the conversation clearly supports it; if you are not confident about a speaker, leave that speaker unassigned with an empty string. Never invent a name that is not in the known profiles list."""
