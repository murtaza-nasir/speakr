"""Every API v1 and webhook route declares its token scope (mailr spec G1).

Scoped tokens are refused on any route without require_scope, so a new route
that forgets one is unusable for scoped tokens; this test fails first, with
the endpoint named. The table is the scope design: change it deliberately.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app

EXPECTED = {
    "read": {"get_openapi_spec", "get_docs", "get_stats", "get_current_user", "list_recordings", "list_recording_changes", "search_recordings_v1", "list_external_refs", "get_recording",
             "get_transcript", "get_summary", "get_notes", "get_recording_status", "list_tags", "list_folders",
             "get_folder", "get_transcription_info", "list_speakers", "get_recording_speakers",
             "get_recording_events", "download_events_ics", "download_audio"},
    "share": {"create_public_share", "list_public_shares", "revoke_public_share"},
    "write": {"replace_recording_tags", "add_external_ref", "replace_external_refs", "delete_external_ref", "update_recording", "replace_notes", "replace_summary", "create_tag", "update_tag",
              "add_tags_to_recording", "remove_tag_from_recording", "create_folder", "update_folder",
              "create_speaker", "update_speaker", "assign_speakers", "batch_update_recordings"},
    "account": {"update_auto_summarization"},
    "upload": {"upload_recording", "upload_from_asr_voice_recorder"},
    "process": {"api_regenerate_title", "identify_speakers", "start_transcription", "start_summarization",
                "chat_with_recording", "batch_transcribe_recordings"},
    "delete": {"delete_recording", "delete_recording_audio", "batch_delete_recordings", "delete_tag",
               "delete_folder", "delete_speaker"},
    "webhooks": {"list_webhooks", "create_webhook", "get_webhook", "update_webhook", "delete_webhook",
                 "rotate_secret", "test_fire", "list_deliveries", "get_delivery", "replay_delivery"},
    "": {"get_current_token", "get_capabilities"},      # any valid token
}


def _actual():
    found = {}
    for rule in app.url_map.iter_rules():
        bp = rule.endpoint.split(".", 1)[0]
        if bp not in ("api_v1", "webhooks"):
            continue
        view = app.view_functions[rule.endpoint]
        name = rule.endpoint.split(".", 1)[1]
        scopes = getattr(view, "_required_scopes", None)
        found[name] = None if scopes is None else ",".join(sorted(scopes))
    return found


def test_every_v1_and_webhook_route_declares_a_scope():
    missing = sorted(name for name, scopes in _actual().items() if scopes is None)
    assert not missing, f"routes without require_scope: {missing}"


def test_scopes_match_the_design_table():
    expected = {name: scope for scope, names in EXPECTED.items() for name in names}
    actual = _actual()
    assert set(actual) == set(expected), (
        f"not in table: {sorted(set(actual) - set(expected))}; gone: {sorted(set(expected) - set(actual))}")
    wrong = {n: (actual[n], expected[n]) for n in actual if actual[n] != expected[n]}
    assert not wrong, f"scope differs (actual, expected): {wrong}"
