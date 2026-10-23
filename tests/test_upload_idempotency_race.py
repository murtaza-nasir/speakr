"""One recording per upload idempotency key, also under overlapping retries (#476).

The endpoint checks the key long before the recording is written, so two
retries can both pass the check. The unique index decides: the loser answers
with the winner's recording and leaves nothing of its own behind.

SHARED-DB: uses the world fixture of test_api_v1_external_refs, which removes
its users, recordings, webhooks and stored media afterwards.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.init_db import unique_upload_idempotency_key
from src.models import Recording, Webhook, WebhookDelivery
from src.services.storage import get_storage_service
from src.services.storage.service import StorageService
from tests.test_api_v1_external_refs import _no_media_tools, _upload, world  # noqa: F401

CODEC = {"has_video": False, "audio_codec": "opus", "video_codec": None, "duration": 5.0}


def test_a_retry_that_overlaps_the_first_upload_replays_it(world):
    with app.app_context():
        hook = Webhook(user_id=world["owner"], name="m", url="https://example.com/h", secret="s" * 40,
                       events=json.dumps(["recording.created"]))
        db.session.add(hook)
        db.session.commit()
        hook_id = hook.id
    staging = get_storage_service().get_staging_dir()
    staged_before = set(os.listdir(staging))
    store = StorageService.upload_local_file
    winner = []

    def _codec(*args, **kwargs):
        # The first request has passed its key check; the retry runs to the end now.
        if not winner:
            winner.append(None)
            winner[0] = _upload(world, idempotency_key="msg-race")
        return CODEC

    with _no_media_tools() as enqueue, \
            patch("src.api.recordings.get_codec_info", side_effect=_codec), \
            patch.object(StorageService, "upload_local_file", autospec=True, side_effect=store) as stored:
        loser = _upload(world, idempotency_key="msg-race")
        assert enqueue.call_count == 1 and stored.call_count == 1

    first = winner[0]
    assert first.status_code == 202
    assert loser.status_code == 200 and loser.get_json()["idempotent_replay"] is True
    assert loser.get_json()["id"] == first.get_json()["id"]
    assert set(os.listdir(staging)) == staged_before
    with app.app_context():
        assert Recording.query.filter_by(user_id=world["owner"], original_filename="memo.webm").count() == 1
        assert WebhookDelivery.query.filter_by(webhook_id=hook_id).count() == 1


def _index(engine, name):
    return next((i for i in inspect(engine).get_indexes("recording") if i["name"] == name), None)


def test_the_migration_keeps_the_newest_of_duplicated_keys(world):
    with app.app_context():
        engine = db.engine
        try:
            # The schema before #476: a plain index that let a key in twice.
            with engine.begin() as conn:
                conn.execute(text("DROP INDEX ux_recording_user_idem"))
                conn.execute(text('CREATE INDEX ix_recording_user_idem ON "recording" '
                                  "(user_id, upload_idempotency_key)"))
            rows = [Recording(user_id=user, title="r", status="COMPLETED", original_filename="r.wav",
                              upload_idempotency_key=key)
                    for user, key in [("owner", "dup"), ("owner", "dup"), ("owner", "dup"),
                                      ("owner", "solo"), ("guest", "dup")]]
            for r, user in zip(rows, ["owner"] * 4 + ["guest"]):
                r.user_id = world[user]
            db.session.add_all(rows)
            db.session.flush()
            ids = [r.id for r in rows]
            db.session.commit()   # an open session would block the DDL on PostgreSQL

            unique_upload_idempotency_key(app, engine)
            unique_upload_idempotency_key(app, engine)   # second start: no-op

            db.session.expire_all()
            keys = [db.session.get(Recording, i).upload_idempotency_key for i in ids]
            assert keys == [None, None, "dup", "solo", "dup"]
            assert _index(engine, "ux_recording_user_idem")["unique"]
            assert _index(engine, "ix_recording_user_idem") is None
            db.session.add(Recording(user_id=world["owner"], title="r", status="COMPLETED",
                                     original_filename="r.wav", upload_idempotency_key="solo"))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()
        finally:
            db.session.rollback()
            unique_upload_idempotency_key(app, engine)
