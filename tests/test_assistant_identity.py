"""The identity line that opens every chat prompt (src/utils/assistant_name.py)."""

from src.utils.assistant_name import assistant_identity


def test_the_assistant_is_software_inside_speakr_with_the_chosen_name():
    text = assistant_identity("Iris", "Jordan Avery", "Product Manager", "Fernhill Labs")
    assert text.startswith("You are Iris, the AI assistant built into Speakr, ")
    assert "not a person" in text and "do not work for the user or their organization" in text
    assert "You are talking with Jordan Avery (Product Manager, Fernhill Labs)." in text
    assert "only to understand the context of their recordings" in text


def test_missing_or_placeholder_profile_fields_are_left_out():
    text = assistant_identity("Ambrose", "the user", "professional", "their organization")
    assert "You are talking with the user." in text
    assert "professional" not in text.split("You are talking with")[1]
    assert "Use their role" not in text


def test_only_the_fields_present_are_listed():
    assert "You are talking with Sam (Acme)." in assistant_identity("Ambrose", "Sam", None, "Acme")
    assert "You are talking with Sam (Editor)." in assistant_identity("Ambrose", "Sam", "Editor", "")
