"""Configurable AI title instructions (#400).

The title prompt follows the summary prompt's precedence: tag > folder >
user > admin default > shipped default. It can be set on tags and folders
(personal and group), in the user's Prompt Options, and by the admin, and the
resolved text is what reaches the model in both prompt layouts.

SHARED-DB: every assertion is scoped to the rows the test created. The admin
setting is global, so tests that change it restore it.
"""

import json
import os
import sys
import uuid
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.tasks.processing as processing
from src.app import app, db
from src.config.prompts import DEFAULT_TITLE_PROMPT
from src.models import User, Recording, Tag, Folder, RecordingTag, SystemSetting

app.config["WTF_CSRF_ENABLED"] = False

TRANSCRIPT = json.dumps([
    {"speaker": "SPEAKER_00", "sentence": "Christine Lagarde opened the press conference on the deposit rate.", "start_time": 0, "end_time": 5},
    {"speaker": "SPEAKER_01", "sentence": "Questions followed on the inflation outlook.", "start_time": 5, "end_time": 9},
])


@pytest.fixture
def ctx():
    with app.app_context():
        yield


@contextmanager
def admin_title_prompt(value):
    previous = SystemSetting.get_setting('admin_default_title_prompt', None)
    SystemSetting.set_setting('admin_default_title_prompt', value, setting_type='string')
    try:
        yield
    finally:
        SystemSetting.set_setting('admin_default_title_prompt', previous if previous is not None else DEFAULT_TITLE_PROMPT,
                                  setting_type='string')


def _user(**kwargs):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"tp_{suffix}", email=f"tp_{suffix}@local.test", password="x", **kwargs)
    db.session.add(user)
    db.session.commit()
    return user


def _recording(user, folder=None):
    rec = Recording(user_id=user.id, title="r", status="COMPLETED", audio_path="local://recordings/x.mp3",
                    original_filename="x.mp3", transcription=TRANSCRIPT, folder_id=folder.id if folder else None)
    db.session.add(rec)
    db.session.commit()
    return rec


def _tag(user, title_prompt=None, order=0, rec=None):
    tag = Tag(name=f"t_{uuid.uuid4().hex[:6]}", user_id=user.id, title_prompt=title_prompt)
    db.session.add(tag)
    db.session.commit()
    if rec is not None:
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=order))
        db.session.commit()
    return tag


def _folder(user, title_prompt=None):
    folder = Folder(name=f"f_{uuid.uuid4().hex[:6]}", user_id=user.id, title_prompt=title_prompt)
    db.session.add(folder)
    db.session.commit()
    return folder


def _client(user):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


# ----------------------------------------------------------------- precedence

def test_shipped_default_when_nothing_is_set(ctx):
    rec = _recording(_user())
    with admin_title_prompt(''):
        assert processing.resolve_title_instructions(rec) == (DEFAULT_TITLE_PROMPT, 'default')


def test_each_level_overrides_the_ones_below(ctx):
    user = _user()
    folder = _folder(user)
    rec = _recording(user, folder)
    with admin_title_prompt('ADMIN'):
        assert processing.resolve_title_instructions(rec) == ('ADMIN', 'admin')

        user.title_prompt = 'USER'
        db.session.commit()
        assert processing.resolve_title_instructions(rec) == ('USER', 'user')

        folder.title_prompt = 'FOLDER'
        db.session.commit()
        assert processing.resolve_title_instructions(rec) == ('FOLDER', 'folder')

        _tag(user, 'TAG', rec=rec)
        assert processing.resolve_title_instructions(rec) == ('TAG', 'tag')


def test_blank_levels_fall_through(ctx):
    user = _user(title_prompt='   ')
    folder = _folder(user, title_prompt='')
    rec = _recording(user, folder)
    _tag(user, '  ', rec=rec)
    with admin_title_prompt('ADMIN'):
        assert processing.resolve_title_instructions(rec) == ('ADMIN', 'admin')


def test_several_tag_prompts_are_joined_in_tag_order(ctx):
    user = _user()
    rec = _recording(user)
    _tag(user, 'SECOND', order=1, rec=rec)
    _tag(user, 'FIRST', order=0, rec=rec)
    _tag(user, None, order=2, rec=rec)
    text, source = processing.resolve_title_instructions(rec)
    assert source == 'tag'
    assert text == 'FIRST\n\nSECOND'


def test_another_users_personal_tag_is_ignored(ctx):
    owner, other = _user(), _user()
    rec = _recording(owner)
    _tag(other, 'NOT YOURS', rec=rec)
    with admin_title_prompt('ADMIN'):
        assert processing.resolve_title_instructions(rec)[1] == 'admin'


# ------------------------------------------------------------ prompt content

def _title_messages(rec, prefix_mode):
    calls = []

    def fake(messages=None, **kwargs):
        calls.append(messages)
        stub = MagicMock()
        stub.choices = [MagicMock()]
        stub.choices[0].message.content = "Lagarde ECB press conference on deposit rate"
        stub.choices[0].message.reasoning = None
        return stub

    with patch.object(processing, "PREFIX_CACHE_OPTIMIZED_PROMPTS", prefix_mode), \
         patch("src.tasks.processing.call_llm_completion", side_effect=fake):
        title = processing._generate_ai_title(rec)
    return title, {m["role"]: m["content"] for m in calls[-1]}


@pytest.mark.parametrize("prefix_mode", [False, True])
def test_resolved_instructions_reach_the_model(ctx, prefix_mode):
    user = _user()
    rec = _recording(user)
    _tag(user, '- Name the institution and the main speaker', rec=rec)
    title, messages = _title_messages(rec, prefix_mode)
    assert title == "Lagarde ECB press conference on deposit rate"
    user_msg = messages["user"]
    assert '- Name the institution and the main speaker' in user_msg
    assert 'Maximum 8 words' not in user_msg
    assert 'Output ONLY the title text' in user_msg
    # The instructions come after the transcript, so the prefix-cache layout
    # keeps the transcript as the shared prefix.
    assert user_msg.index('Christine Lagarde') < user_msg.index('Name the institution')


def test_default_instructions_keep_the_previous_prompt(ctx):
    rec = _recording(_user())
    with admin_title_prompt(''):
        _, messages = _title_messages(rec, False)
    assert '- Maximum 8 words' in messages["user"]


def test_incognito_titles_use_the_user_level(ctx):
    user = _user(title_prompt='- Start with the date')
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs["messages"])
        stub = MagicMock()
        stub.choices = [MagicMock()]
        stub.choices[0].message.content = "A title"
        return stub

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = fake_create
    with patch("src.tasks.processing.client", new=fake_client):
        processing._generate_incognito_title(TRANSCRIPT, user=user)
    assert '- Start with the date' in calls[-1][1]["content"]


# ------------------------------------------------------------- where it is set

def test_tag_and_folder_routes_store_the_title_prompt(ctx):
    user = _user()
    c = _client(user)
    r = c.post('/api/tags', json={'name': f'tag_{uuid.uuid4().hex[:6]}', 'title_prompt': 'TAG TITLE'})
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    tag_id = (r.get_json().get('tag') or r.get_json())['id']
    assert db.session.get(Tag, tag_id).title_prompt == 'TAG TITLE'
    c.put(f'/api/tags/{tag_id}', json={'title_prompt': ''})
    db.session.expire_all()
    assert db.session.get(Tag, tag_id).title_prompt is None

    previous = SystemSetting.get_setting('enable_folders', False)
    SystemSetting.set_setting('enable_folders', 'true', setting_type='boolean')
    try:
        r = c.post('/api/folders', json={'name': f'folder_{uuid.uuid4().hex[:6]}', 'title_prompt': 'FOLDER TITLE'})
    finally:
        SystemSetting.set_setting('enable_folders', 'true' if previous else 'false', setting_type='boolean')
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    body = r.get_json()
    folder_id = (body.get('folder') or body)['id']
    assert db.session.get(Folder, folder_id).title_prompt == 'FOLDER TITLE'
    assert db.session.get(Folder, folder_id).to_dict()['title_prompt'] == 'FOLDER TITLE'


def test_account_prompt_form_saves_and_keeps_the_title_prompt(ctx):
    user = _user()
    c = _client(user)
    c.post('/account', data={'summary_prompt': '', 'title_prompt': '  - Speaker and topic  '})
    db.session.expire_all()
    assert db.session.get(User, user.id).title_prompt == '- Speaker and topic'
    # A submission of the same form without the field leaves it alone.
    c.post('/account', data={'summary_prompt': ''})
    db.session.expire_all()
    assert db.session.get(User, user.id).title_prompt == '- Speaker and topic'


def test_admin_default_is_seeded(ctx):
    assert SystemSetting.query.filter_by(key='admin_default_title_prompt').first() is not None
