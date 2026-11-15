"""Webhook delivery records are pruned, and titles can be left out of payloads
(privacy review A2).

webhook_delivery rows were never removed, so the payload of every event (the
recording id, its title, the original filename, error text) stayed in the
database without limit. Records older than webhook_delivery_retention_days
(WEBHOOK_DELIVERY_RETENTION_DAYS, default 30) are now removed by the hourly
cleanup; 0 keeps them. With webhook_omit_titles (WEBHOOK_OMIT_TITLES, default
off) the title and the original filename are left out of payloads.

SHARED-DB: the user, the webhook, its deliveries and the settings are restored.
"""

import json
import os
import secrets
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, SystemSetting, User, Webhook, WebhookDelivery
from src.services import webhook_dispatch as wd
from src.utils.system_settings import ENV_BACKED_SETTINGS

KEYS = ('webhook_delivery_retention_days', 'webhook_omit_titles')


@pytest.fixture
def world():
    with app.app_context():
        saved = {}
        for key in KEYS:
            row = SystemSetting.query.filter_by(key=key).first()
            saved[key] = (row.value, row.setting_type) if row else None
            SystemSetting.set_setting(key, None, ENV_BACKED_SETTINGS[key][3], ENV_BACKED_SETTINGS[key][2])
        s = secrets.token_hex(4)
        user = User(username=f"whr_{s}", email=f"whr_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title="Heron title marker", status="COMPLETED",
                        original_filename="heron-filename-marker.wav")
        hook = Webhook(user_id=user.id, name="h", url="https://example.com/h", secret="s" * 40,
                       events=json.dumps(["recording.created", "recording.transcription.failed"]))
        db.session.add_all([rec, hook])
        db.session.commit()
        ids = dict(user=user.id, rec=rec.id, hook=hook.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        WebhookDelivery.query.filter_by(webhook_id=ids["hook"]).delete()
        Webhook.query.filter_by(id=ids["hook"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        User.query.filter_by(id=ids["user"]).delete()
        db.session.commit()
        for key, value in saved.items():
            if value is None:
                row = SystemSetting.query.filter_by(key=key).first()
                if row:
                    db.session.delete(row)
                    db.session.commit()
            else:
                SystemSetting.set_setting(key, value[0], None, value[1])


def _delivery(hook_id, age_days, status):
    row = WebhookDelivery(webhook_id=hook_id, event_id=secrets.token_hex(8), event_type="recording.created",
                          payload=json.dumps({"data": {"title": "Heron title marker"}}), status=status,
                          created_at=datetime.utcnow() - timedelta(days=age_days))
    db.session.add(row)
    db.session.commit()
    return row.id


def _left(hook_id):
    return {d.id for d in WebhookDelivery.query.filter_by(webhook_id=hook_id)}


def test_defaults():
    assert ENV_BACKED_SETTINGS['webhook_delivery_retention_days'][:3] == ('WEBHOOK_DELIVERY_RETENTION_DAYS', 30, 'integer')
    assert ENV_BACKED_SETTINGS['webhook_omit_titles'][:3] == ('WEBHOOK_OMIT_TITLES', False, 'boolean')


def test_records_older_than_the_retention_are_removed_whatever_their_state(world, monkeypatch):
    monkeypatch.delenv('WEBHOOK_DELIVERY_RETENTION_DAYS', raising=False)
    with app.app_context():
        old = [_delivery(world["hook"], 31, status)
               for status in ("success", "permanent_failure", "failed", "pending")]
        recent = [_delivery(world["hook"], 29, status)
                  for status in ("success", "permanent_failure", "failed", "pending")]
        assert wd.delivery_retention_days() == 30
        assert wd.prune_deliveries() == len(old)
        assert _left(world["hook"]) == set(recent)
        assert wd.prune_deliveries() == 0


def test_zero_keeps_every_record(world, monkeypatch):
    monkeypatch.setenv('WEBHOOK_DELIVERY_RETENTION_DAYS', '0')
    with app.app_context():
        kept = _delivery(world["hook"], 4000, "success")
        assert wd.prune_deliveries() == 0
        assert _left(world["hook"]) == {kept}


def test_env_value_applies_and_a_saved_setting_overrides_it(world, monkeypatch):
    monkeypatch.setenv('WEBHOOK_DELIVERY_RETENTION_DAYS', '7')
    with app.app_context():
        eight = _delivery(world["hook"], 8, "success")
        six = _delivery(world["hook"], 6, "success")
        SystemSetting.set_setting('webhook_delivery_retention_days', '10', setting_type='integer')
        assert wd.prune_deliveries() == 0, "the saved value of 10 days applies"
        SystemSetting.set_setting('webhook_delivery_retention_days', None, setting_type='integer')
        assert wd.prune_deliveries() == 1, "back to 7 days from .env"
        assert _left(world["hook"]) == {six}
        assert eight not in _left(world["hook"])


def test_a_large_backlog_is_removed_in_batches(world, monkeypatch):
    monkeypatch.delenv('WEBHOOK_DELIVERY_RETENTION_DAYS', raising=False)
    with app.app_context():
        for _ in range(7):
            _delivery(world["hook"], 90, "success")
        keep = _delivery(world["hook"], 1, "success")
        assert wd.prune_deliveries(batch_size=3) == 7
        assert _left(world["hook"]) == {keep}


def test_deliveries_of_other_webhooks_are_pruned_by_the_same_rule(world, monkeypatch):
    """The cleanup has no owner filter: the rule is the age of the record."""
    monkeypatch.delenv('WEBHOOK_DELIVERY_RETENTION_DAYS', raising=False)
    with app.app_context():
        mine = _delivery(world["hook"], 45, "success")
        wd.prune_deliveries()
        assert db.session.get(WebhookDelivery, mine) is None


def _payloads(world, event_type, data):
    with app.app_context():
        wd.enqueue_event(db.session, world["user"], event_type, data)
        return [json.loads(d.payload)["data"]
                for d in WebhookDelivery.query.filter_by(webhook_id=world["hook"]).order_by(WebhookDelivery.id)]


def test_payloads_carry_the_title_and_filename_by_default(world, monkeypatch):
    monkeypatch.delenv('WEBHOOK_OMIT_TITLES', raising=False)
    data = {'recording_id': world["rec"], 'title': "Heron title marker",
            'original_filename': "heron-filename-marker.wav", 'status': 'PENDING'}
    (payload,) = _payloads(world, 'recording.created', data)
    assert payload['title'] == "Heron title marker" and payload['original_filename'] == "heron-filename-marker.wav"


@pytest.mark.parametrize("how", ["env", "setting"])
def test_title_and_filename_are_left_out_when_asked(world, monkeypatch, how):
    if how == "env":
        monkeypatch.setenv('WEBHOOK_OMIT_TITLES', 'true')
    else:
        monkeypatch.setenv('WEBHOOK_OMIT_TITLES', 'false')
        with app.app_context():
            SystemSetting.set_setting('webhook_omit_titles', 'true', setting_type='boolean')
    data = {'recording_id': world["rec"], 'title': "Heron title marker",
            'original_filename': "heron-filename-marker.wav", 'status': 'PENDING'}
    (created,) = _payloads(world, 'recording.created', data)
    assert 'title' not in created and 'original_filename' not in created
    assert created['recording_id'] == world["rec"] and created['status'] == 'PENDING'
    failed = _payloads(world, 'recording.transcription.failed',
                       {'recording_id': world["rec"], 'title': "Heron title marker", 'error': 'status 500'})[-1]
    assert 'title' not in failed and failed['error'] == 'status 500'
    with app.app_context():
        stored = "\n".join(d.payload for d in WebhookDelivery.query.filter_by(webhook_id=world["hook"]))
    assert "Heron" not in stored and "heron" not in stored
    # The caller's dictionary is not changed.
    assert data['title'] == "Heron title marker"
