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
