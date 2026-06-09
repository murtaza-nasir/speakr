"""Guards for the consolidated default summary prompt.

The default summary prompt used to be copy-pasted in four places (init_db seed,
two processing.py fallbacks, the account-page render). They now all reference
src.config.prompts.DEFAULT_SUMMARY_PROMPT. These tests make sure the constant
stays meaningful and that a fresh database seeds exactly that value, so the
single source of truth can't silently drift from what installs actually get.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.prompts import DEFAULT_SUMMARY_PROMPT


def test_default_summary_prompt_is_meaningful():
    """The constant must be non-trivial markdown with the expected sections, so
    an accidental empty/garbled edit is caught."""
    assert isinstance(DEFAULT_SUMMARY_PROMPT, str)
    assert DEFAULT_SUMMARY_PROMPT.strip(), "default summary prompt must not be empty"
    for section in ("Minutes", "Key Issues Discussed", "next steps", "responsible party"):
        assert section in DEFAULT_SUMMARY_PROMPT, f"missing expected section: {section}"


def test_fresh_install_seeds_the_constant():
    """initialize_database (run once by conftest) must seed
    admin_default_summary_prompt with exactly the shared constant — i.e. a fresh
    install ships the constant, not a stale hardcoded copy."""
    from src.app import app
    from src.models import SystemSetting

    with app.app_context():
        seeded = SystemSetting.get_setting('admin_default_summary_prompt', None)
        assert seeded == DEFAULT_SUMMARY_PROMPT, (
            "the seeded admin_default_summary_prompt drifted from "
            "src.config.prompts.DEFAULT_SUMMARY_PROMPT"
        )


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
