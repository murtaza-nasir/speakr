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
DEFAULT_SUMMARY_PROMPT = """Generate a comprehensive summary that includes the following sections:
- **Key Issues Discussed**: A bulleted list of the main topics
- **Key Decisions Made**: A bulleted list of any decisions reached
- **Action Items**: A bulleted list of tasks assigned, including who is responsible if mentioned"""
