#!/usr/bin/env python3
"""Ambrose search: the recording chat's metadata block, tag and folder
filters on the library tools, the names line in the agent prompt,
find_similar_recordings and search_text.

No network, no real LLM.
"""

import json
import os
import secrets
import sys
from datetime import datetime

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import (Folder, Group, GroupMembership, InternalShare, Recording,  # noqa: E402
                        RecordingTag, Tag, TranscriptChunk, User)
from src.services import inquire_agent as ia  # noqa: E402
from src.services.assistant_present import step_label, step_result, summary_line  # noqa: E402
from src.services.embeddings import serialize_embedding  # noqa: E402
from src.services.library_labels import LibraryLabels, capped  # noqa: E402
from src.services.recording_chat import IncognitoTranscript, build_chat_messages  # noqa: E402
from src.transcript_chunking import chunk_transcript_segments  # noqa: E402

NESTED = hasattr(Folder, 'parent_id')
needs_nested = pytest.mark.skipif(not NESTED, reason='nested folders (#585) are not on this branch')


def _segments(*lines):
    return json.dumps([{'speaker': speaker, 'sentence': text, 'start_time': i * 10.0, 'end_time': i * 10.0 + 8}
                       for i, (speaker, text) in enumerate(lines)])


R1_TRANSCRIPT = _segments(
    ('Dana', 'Good morning everyone.'),
    ('Omar', 'The budget freeze applies to travel this quarter.'),
    ('Dana', 'I am 100% sure about that.'),
    ('Omar', 'The export is called file_name dot csv.'),
    ('Dana', 'The other one is filexname and the rate is 10x.'),
)

EVERY = {'summaries': True, 'notes': True}


def _chunks(rec, vectors=None):
    for i, chunk in enumerate(chunk_transcript_segments(rec.transcription, max_chunk_chars=120)):
        vector = vectors[i % len(vectors)] if vectors else None
        db.session.add(TranscriptChunk(recording_id=rec.id, user_id=rec.user_id, chunk_index=i,
                                       content=chunk['text'], speaker_name=chunk['speaker_name'],
                                       start_time=chunk['start_time'], end_time=chunk['end_time'],
                                       embedding=serialize_embedding(np.array(vector, dtype=np.float32))
                                       if vector is not None else None))


@pytest.fixture
def lib():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f'as_o_{s}', email=f'as_o_{s}@local.test', password='x', name='Morgan Lee',
                     timezone='America/Chicago')
        member = User(username=f'as_m_{s}', email=f'as_m_{s}@local.test', password='x', timezone='UTC')
        other = User(username=f'as_x_{s}', email=f'as_x_{s}@local.test', password='x')
        db.session.add_all([owner, member, other])
        db.session.flush()
        group = Group(name=f'as_group_{s}')
        db.session.add(group)
        db.session.flush()
        db.session.add_all([GroupMembership(group_id=group.id, user_id=owner.id),
                            GroupMembership(group_id=group.id, user_id=member.id)])

        projects = Folder(name='Projects', user_id=owner.id)
        db.session.add(projects)
        db.session.flush()
        alpha = Folder(name='Alpha', user_id=owner.id)
        if NESTED:
            alpha.parent_id = projects.id
        team = Folder(name=f'Team {s}', user_id=owner.id, group_id=group.id)
        secret = Folder(name=f'Secret {s}', user_id=other.id)
        db.session.add_all([alpha, team, secret])
        db.session.flush()

        budget = Tag(name='Budget', user_id=owner.id)
        shared = Tag(name=f'Shared {s}', user_id=owner.id, group_id=group.id)
        mine = Tag(name=f'MineOnly {s}', user_id=member.id)
        hidden = Tag(name=f'Hidden {s}', user_id=other.id)
        db.session.add_all([budget, shared, mine, hidden])
        db.session.flush()

        r1 = Recording(user_id=owner.id, title='Weekly Sync', status='COMPLETED', participants='Dana, Omar',
                       transcription=R1_TRANSCRIPT, meeting_date=datetime(2026, 8, 20, 15, 0),
                       audio_duration_seconds=3725, folder_id=alpha.id, notes='Owner notes about the rocket plan')
        r2 = Recording(user_id=owner.id, title='Budget review', status='COMPLETED', participants='Dana (finance), Lee',
                       transcription=_segments(('Dana', 'Travel stays frozen.'), ('Lee', 'Hiring too.')),
                       summary='The freeze on hiring and travel.', meeting_date=datetime(2026, 8, 22, 15, 0),
                       folder_id=projects.id)
        r3 = Recording(user_id=owner.id, title='Garden notes', status='COMPLETED', participants='Pat',
                       transcription=_segments(('Pat', 'Tomatoes need water.')),
                       meeting_date=datetime(2026, 1, 2, 15, 0))
        r4 = Recording(user_id=owner.id, title='Team retro', status='COMPLETED', participants='Sam',
                       transcription=_segments(('Sam', 'The rocket plan worked.')), notes='rocket notes of the owner',
                       meeting_date=datetime(2026, 8, 25, 15, 0), folder_id=team.id)
        rx = Recording(user_id=other.id, title='Foreign rocket', status='COMPLETED', participants='Dana',
                       transcription=_segments(('Dana', 'The budget freeze applies to travel.')),
                       meeting_date=datetime(2026, 8, 21, 15, 0), folder_id=secret.id)
        db.session.add_all([r1, r2, r3, r4, rx])
        db.session.flush()
        for rec, tag, order in ((r1, budget, 0), (r2, budget, 0), (r2, shared, 1), (r4, budget, 0),
                                (r4, shared, 1), (r4, mine, 2), (rx, hidden, 0)):
            db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=order))
        db.session.add(InternalShare(recording_id=r4.id, owner_id=owner.id, shared_with_user_id=member.id,
                                     source_type='group_folder', source_folder_id=team.id))
        for rec in (r1, r2, r3, r4, rx):
            _chunks(rec)
        db.session.commit()
        ids = dict(owner=owner.id, member=member.id, other=other.id, group=group.id, projects=projects.id,
                   alpha=alpha.id, team=team.id, secret=secret.id, r1=r1.id, r2=r2.id, r3=r3.id, r4=r4.id,
                   rx=rx.id, s=s, tags=[budget.id, shared.id, mine.id, hidden.id])
    yield ids
    with app.app_context():
        db.session.rollback()
        recs = [ids[k] for k in ('r1', 'r2', 'r3', 'r4', 'rx')]
        TranscriptChunk.query.filter(TranscriptChunk.recording_id.in_(recs)).delete(synchronize_session=False)
        InternalShare.query.filter(InternalShare.recording_id.in_(recs)).delete(synchronize_session=False)
        RecordingTag.query.filter(RecordingTag.recording_id.in_(recs)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(recs)).delete(synchronize_session=False)
        Tag.query.filter(Tag.id.in_(ids['tags'])).delete(synchronize_session=False)
        Folder.query.filter(Folder.id.in_([ids['alpha'], ids['team'], ids['secret']])).delete(synchronize_session=False)
        Folder.query.filter_by(id=ids['projects']).delete()
        GroupMembership.query.filter_by(group_id=ids['group']).delete()
        Group.query.filter_by(id=ids['group']).delete()
        User.query.filter(User.id.in_([ids['owner'], ids['member'], ids['other']])).delete(synchronize_session=False)
        db.session.commit()


def _owner_ctx(lib, availability=EVERY, extra=()):
    return ia.ToolContext(lib['owner'], {}, {lib[k] for k in ('r1', 'r2', 'r3', 'r4')} | set(extra), availability)


def _member_ctx(lib, availability=EVERY):
    return ia.ToolContext(lib['member'], {}, {lib['r4']}, availability)


def _alpha_path():
    return 'Projects / Alpha' if NESTED else 'Alpha'


# ---------------------------------------------------------------------------
# Recording chat metadata block
# ---------------------------------------------------------------------------

def _system(rec_id, user_id, **kw):
    messages, _ = build_chat_messages(db.session.get(Recording, rec_id), db.session.get(User, user_id), 'Q?', [], **kw)
    return messages[0]['content']


def test_recording_prompt_names_the_recording(lib):
    with app.app_context():
        prompt = _system(lib['r1'], lib['owner'])
        block = prompt.split('About this recording:\n')[1].split('\n\n')[0]
        # 15:00 UTC is 10:00 in Chicago in August
        assert block.splitlines() == [
            'Title: Weekly Sync', 'Date: 2026-08-20 10:00', 'Duration: 1 h 2 min',
            f'Folder: {_alpha_path()}', 'Tags: Budget', 'Speakers: Dana, Omar']
        # The same block with segment markers on
        assert 'Speakers: Dana, Omar' in _system(lib['r1'], lib['owner'], with_sources=True)
        assert 'meeting' not in block.lower()


def test_recording_prompt_leaves_out_empty_lines(lib):
    with app.app_context():
        block = _system(lib['r3'], lib['owner']).split('About this recording:\n')[1].split('\n\n')[0]
        assert block.splitlines() == ['Title: Garden notes', 'Date: 2026-01-02 09:00', 'Speakers: Pat']


def test_recording_prompt_shows_only_what_the_viewer_sees(lib):
    with app.app_context():
        owner_view = _system(lib['r4'], lib['owner'])
        member_view = _system(lib['r4'], lib['member'])
        s = lib['s']
        assert f'Tags: Budget, Shared {s}\n' in owner_view and 'MineOnly' not in owner_view
        # The member sees the group tag and their own tag, not the owner's personal tag
        assert f'Tags: Shared {s}, MineOnly {s}\n' in member_view
        assert f'Folder: Team {s}' in member_view and 'Date: 2026-08-25 15:00' in member_view
        # The owner's personal folder is never shown to a recipient
        r1 = db.session.get(Recording, lib['r1'])
        labels = LibraryLabels(lib['member'], [r1.id])
        assert labels.folder_path_of(r1.id) is None and labels.tags_of(r1.id) == []


def test_incognito_prompt_has_no_metadata_block():
    with app.app_context():
        user = User(username='x', email='x@x', password='x')
        user.id = 0
        messages, _ = build_chat_messages(IncognitoTranscript(R1_TRANSCRIPT), user, 'Q?', [])
        assert 'About this recording' not in messages[0]['content']


# ---------------------------------------------------------------------------
# Tag and folder filters
# ---------------------------------------------------------------------------

def _titles(res, key='recordings'):
    return sorted(r['title'] for r in res[key])


def test_list_filters_by_tag_any_case(lib):
    with app.app_context():
        ctx = _owner_ctx(lib)
        assert _titles(ia.tool_list_recordings(ctx, {'tags': ['budget']})) == ['Budget review', 'Team retro',
                                                                               'Weekly Sync']
        shared = f"SHARED {lib['s']}"
        assert _titles(ia.tool_list_recordings(ctx, {'tags': [shared]})) == ['Budget review', 'Team retro']
        res = ia.tool_list_recordings(ctx, {'tags': ['nope']})
        assert res['total_in_scope'] == 0 and 'nope' in res['note']
        row = next(r for r in ia.tool_list_recordings(ctx, {})['recordings'] if r['title'] == 'Weekly Sync')
        assert row['tags'] == ['Budget'] and row['folder'] == _alpha_path()
        garden = next(r for r in ia.tool_list_recordings(ctx, {})['recordings'] if r['title'] == 'Garden notes')
        assert 'tags' not in garden and 'folder' not in garden


def test_list_filters_by_folder_name_or_path(lib):
    with app.app_context():
        ctx = _owner_ctx(lib)
        assert _titles(ia.tool_list_recordings(ctx, {'folder': 'alpha'})) == ['Weekly Sync']
        both = ia.tool_list_recordings(ctx, {'folder': 'projects', 'tags': ['Budget']})
        assert _titles(both) == (['Budget review', 'Weekly Sync'] if NESTED else ['Budget review'])
        res = ia.tool_list_recordings(ctx, {'folder': 'Nowhere'})
        assert res['total_in_scope'] == 0 and 'Nowhere' in res['note']


@needs_nested
def test_folder_filter_includes_subfolders_and_full_paths(lib):
    with app.app_context():
        ctx = _owner_ctx(lib)
        assert _titles(ia.tool_list_recordings(ctx, {'folder': 'Projects'})) == ['Budget review', 'Weekly Sync']
        assert _titles(ia.tool_list_recordings(ctx, {'folder': 'projects/ALPHA'})) == ['Weekly Sync']
        assert _titles(ia.tool_list_recordings(ctx, {'folder': 'Projects / Alpha'})) == ['Weekly Sync']
        assert ia.tool_list_recordings(ctx, {'folder': 'Alpha / Projects'})['total_in_scope'] == 0
        assert ctx.labels.relation(lib['r2'], lib['r1']) == 'parent folder'
        assert ctx.labels.relation(lib['r1'], lib['r2']) == 'subfolder'


def test_search_transcripts_narrows_by_tag_and_folder(lib, monkeypatch):
    with app.app_context():
        seen = {}

        def fake_search(uid, query, filters, top_k):
            seen['ids'] = set(filters['recording_ids'])
            r1 = db.session.get(Recording, lib['r1'])
            chunk = TranscriptChunk.query.filter_by(recording_id=r1.id).first()
            return [(chunk, 0.9)]

        monkeypatch.setattr(ia, 'semantic_search_chunks', fake_search)
        ctx = _owner_ctx(lib)
        res = ia.tool_search_transcripts(ctx, {'query': 'freeze', 'tags': ['Budget'], 'folder': 'Alpha'})
        assert seen['ids'] == {lib['r1']}
        assert res['results'][0]['tags'] == ['Budget'] and res['results'][0]['folder'] == _alpha_path()
        seen.clear()
        res = ia.tool_search_transcripts(ctx, {'query': 'freeze', 'tags': ['missing']})
        assert seen == {} and res['result_count'] == 0 and 'missing' in res['note']


def test_metadata_has_visible_tags_and_folder(lib):
    with app.app_context():
        meta = ia.tool_get_recording_metadata(_member_ctx(lib), {'recording_id': lib['r4']})
        s = lib['s']
        assert meta['tags'] == [f'Shared {s}', f'MineOnly {s}'] and meta['folder'] == f'Team {s}'


def test_other_users_tags_and_folders_never_appear(lib):
    with app.app_context():
        # Even with the foreign recording in scope, its owner's tag and folder stay hidden
        ctx = _owner_ctx(lib, extra=[lib['rx']])
        s = lib['s']
        meta = ia.tool_get_recording_metadata(ctx, {'recording_id': lib['rx']})
        assert 'tags' not in meta and 'folder' not in meta
        line = ia._names_line(ctx.labels, None)
        assert f'Hidden {s}' not in line and f'Secret {s}' not in line
        assert ia.tool_list_recordings(ctx, {'tags': [f'Hidden {s}']})['total_in_scope'] == 0
        assert ia.tool_list_recordings(ctx, {'folder': f'Secret {s}'})['total_in_scope'] == 0
        # The member sees only the shared recording's group labels
        member_line = ia._names_line(_member_ctx(lib).labels, None)
        assert f'Shared {s}' in member_line and f'Team {s}' in member_line and f'MineOnly {s}' in member_line
        assert 'Budget' not in member_line and 'Projects' not in member_line
        # Without a recording in scope there is nothing to list
        assert ia._names_line(ia.ToolContext(lib['other'], {}, set(), EVERY).labels, None) == ''


# ---------------------------------------------------------------------------
# Names line
# ---------------------------------------------------------------------------

class _Labels:
    def __init__(self, tags, folders):
        self._tags, self._folders = tags, folders

    def tag_names_in_use(self):
        return self._tags

    def folder_paths_in_use(self):
        return self._folders


def test_names_line_is_capped_and_most_used_first(lib):
    with app.app_context():
        line = ia._names_line(_Labels([f't{i}' for i in range(75)], [f'F{i}' for i in range(61)]), None)
        assert 't59 and 15 more.' in line and 't60' not in line
        assert 'F59 and 1 more.' in line
        assert 'pass these exact names in `tags` or `folder`' in line
        assert capped(['a', 'b'], 2) == 'a, b'
        # Not offered when no tool filters by tags or folders
        assert ia._names_line(_Labels(['a'], []), ['get_transcript']) == ''
        names = _owner_ctx(lib).labels.tag_names_in_use()
        assert names[0] == 'Budget'  # on three recordings
        folders = _owner_ctx(lib).labels.folder_paths_in_use()
        assert set(folders) >= {'Projects', _alpha_path(), f"Team {lib['s']}"}


def test_prompt_carries_names_and_search_guidance():
    ctx = {'name': 'A', 'title': 'x', 'company': 'y', 'assistant_name': 'Iris'}
    prompt = ia._system_prompt(ctx, '', EVERY, '', None, 'Tags on these recordings: Budget.')
    assert 'Tags on these recordings: Budget.' in prompt
    assert 'call find_similar_recordings first' in prompt
    assert 'Use search_text for exact words' in prompt and 'search_transcripts for topics' in prompt
    assert prompt.count('You have tools to search transcripts, search for exact words, list recordings, '
                        'find similar recordings, look up people, and read transcripts, stored summaries') == 1
    narrow = ia._system_prompt(ctx, '', EVERY, '', ['search_transcripts', 'get_transcript'])
    assert 'search_text' not in narrow and 'find_similar' not in narrow


def test_agent_loop_sends_the_names_line(lib, monkeypatch):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_inquire_agent import FakeLLM, text_chunk, USER_CTX
    monkeypatch.setenv('INQUIRE_AGENT_TOOL_MODE', 'native')
    fake = FakeLLM([[text_chunk('Done. ' * 50)]])
    list(ia.run_inquire_agent(app, lib['owner'], USER_CTX, 'what is tagged?', [], None, {}, llm_call=fake))
    system = fake.calls[0]['messages'][0]['content']
    assert 'Tags on these recordings: Budget' in system and 'Folders: ' in system
    names = [t['function']['name'] for t in fake.calls[0]['tools']]
    assert {'search_text', 'find_similar_recordings'} <= set(names)


# ---------------------------------------------------------------------------
# find_similar_recordings
# ---------------------------------------------------------------------------

def test_find_similar_ranks_with_reasons(lib):
    with app.app_context():
        res = ia.tool_find_similar_recordings(_owner_ctx(lib), {'recording_id': lib['r1']})
        ids = [r['recording_id'] for r in res['results']]
        assert lib['r1'] not in ids
        assert ids[0] == lib['r2']
        top = res['results'][0]
        assert top['reasons']['shared_speakers'] == ['Dana']  # "Dana (finance)" is Dana
        assert top['reasons']['shared_tags'] == ['Budget']
        assert top['reasons']['days_apart'] == 2
        if NESTED:
            assert top['reasons']['folder'] == 'subfolder'
        assert ids.index(lib['r2']) < ids.index(lib['r3'])
        garden = next(r for r in res['results'] if r['recording_id'] == lib['r3'])
        assert 'shared_speakers' not in garden['reasons'] and garden['reasons']['days_apart'] == 230
        assert res['weights'] == ia.SIMILARITY_WEIGHTS
        assert res['content_method'] == 'words'
        assert ia.tool_find_similar_recordings(_owner_ctx(lib), {'recording_id': lib['r1'], 'limit': 1})[
            'result_count'] == 1
        assert 'error' in ia.tool_find_similar_recordings(_owner_ctx(lib), {'recording_id': lib['rx']})


def test_find_similar_uses_embeddings_when_stored(lib):
    with app.app_context():
        for rid, vector in ((lib['r1'], [1, 0, 0]), (lib['r3'], [0.9, 0.1, 0]), (lib['r2'], [0, 1, 0])):
            for chunk in TranscriptChunk.query.filter_by(recording_id=rid):
                chunk.embedding = np.array(vector, dtype=np.float32).tobytes()
        db.session.commit()
        res = ia.tool_find_similar_recordings(_owner_ctx(lib), {'recording_id': lib['r1']})
        assert res['content_method'] == 'embeddings'
        by_id = {r['recording_id']: r for r in res['results']}
        assert by_id[lib['r3']]['reasons']['content_similarity'] > 0.99
        assert by_id[lib['r2']]['reasons']['content_similarity'] == 0
        assert 'content_similarity' not in by_id[lib['r4']]['reasons']  # no vectors


def test_find_similar_with_nothing_else_in_scope(lib):
    with app.app_context():
        ctx = ia.ToolContext(lib['owner'], {}, {lib['r1']}, EVERY)
        res = ia.tool_find_similar_recordings(ctx, {'recording_id': lib['r1']})
        assert res['result_count'] == 0 and res['results'] == []


# ---------------------------------------------------------------------------
# search_text
# ---------------------------------------------------------------------------

def _hits(res, field=None):
    return [(r['title'], r['field']) for r in res['results'] if field is None or r['field'] == field]


def test_search_text_phrase_and_all_words(lib):
    with app.app_context():
        ctx = _owner_ctx(lib)
        res = ia.tool_search_text(ctx, {'query': 'Freeze applies to TRAVEL'})
        assert _hits(res) == [('Weekly Sync', 'transcript')]
        hit = res['results'][0]
        assert hit['speaker'] == 'Omar' and hit['time'] == '0:10' and hit['seek_seconds'] == 10
        assert hit['snippet'] == 'The budget freeze applies to travel this quarter.'
        assert hit['tags'] == ['Budget'] and hit['meeting_date'] == '2026-08-20'
        assert ia.tool_search_text(ctx, {'query': 'travel freeze'})['result_count'] == 0
        words = ia.tool_search_text(ctx, {'query': 'travel freeze', 'match': 'all_words'})
        assert ('Weekly Sync', 'transcript') in _hits(words)
        # "The freeze on hiring and travel." is a summary
        assert ('Budget review', 'summary') in _hits(words)
        # The foreign recording is out of scope
        assert 'Foreign rocket' not in {t for t, _f in _hits(words)}


def test_search_text_fields(lib):
    with app.app_context():
        ctx = _owner_ctx(lib)
        assert _hits(ia.tool_search_text(ctx, {'query': 'budget', 'fields': ['title']})) == [('Budget review', 'title')]
        notes = ia.tool_search_text(ctx, {'query': 'rocket plan', 'fields': ['notes']})
        assert _hits(notes) == [('Weekly Sync', 'notes')]
        transcript = ia.tool_search_text(ctx, {'query': 'rocket plan', 'fields': ['transcript']})
        assert _hits(transcript) == [('Team retro', 'transcript')]
        assert ia.tool_search_text(ctx, {'query': 'freeze', 'tags': ['Budget'], 'folder': 'Projects',
                                         'fields': ['summary']})['result_count'] == 1
        assert ia.tool_search_text(ctx, {'query': 'travel', 'speakers': ['Pat']})['result_count'] == 0
        assert ia.tool_search_text(ctx, {'query': 'travel', 'date_from': '2026-08-21'})['results'][0][
            'title'] == 'Budget review'


def test_search_text_notes_only_own(lib):
    with app.app_context():
        # The member can read r4 but its notes are the owner's
        res = ia.tool_search_text(_member_ctx(lib), {'query': 'rocket'})
        assert _hits(res) == [('Team retro', 'transcript')]
        owner = ia.tool_search_text(_owner_ctx(lib), {'query': 'rocket notes'})
        assert _hits(owner) == [('Team retro', 'notes')]


def test_search_text_summary_and_notes_gating(lib):
    with app.app_context():
        ctx = _owner_ctx(lib, availability={'summaries': False, 'notes': False})
        res = ia.tool_search_text(ctx, {'query': 'freeze on hiring'})
        assert res['fields'] == ['transcript', 'title'] and res['result_count'] == 0
        res = ia.tool_search_text(ctx, {'query': 'freeze', 'fields': ['summary', 'transcript']})
        assert 'summary' in res['note'] and res['fields'] == ['transcript']
        assert 'error' in ia.tool_search_text(ctx, {'query': 'rocket', 'fields': ['notes']})
        schema = next(s for s in ia.build_toolbox({'summaries': False, 'notes': False})[0]
                      if s['function']['name'] == 'search_text')
        assert schema['function']['parameters']['properties']['fields']['items']['enum'] == ['transcript', 'title']
        assert 'summaries' not in schema['function']['description']


def test_search_text_escapes_like_wildcards(lib):
    with app.app_context():
        ctx = _owner_ctx(lib, availability={'summaries': False, 'notes': False})
        assert [r['snippet'] for r in ia.tool_search_text(ctx, {'query': '100%'})['results']] == [
            'I am 100% sure about that.']
        assert ia.tool_search_text(ctx, {'query': '10%'})['result_count'] == 0
        # '_' is literal: file_name, never filexname
        hits = ia.tool_search_text(ctx, {'query': 'file_name'})['results']
        assert [h['snippet'] for h in hits] == ['The export is called file_name dot csv.']
        assert ia.tool_search_text(ctx, {'query': 'file%name'})['result_count'] == 0
        assert ia.tool_search_text(ctx, {'query': '%'})['result_count'] == 1
        assert 'error' in ia.tool_search_text(ctx, {'query': '  '})


def test_search_text_reads_transcripts_that_have_no_chunks(lib):
    with app.app_context():
        TranscriptChunk.query.filter_by(recording_id=lib['r3']).delete()
        rec = db.session.get(Recording, lib['r3'])
        rec.transcription = _segments(('Pat', 'Tomatoes need water.'), ('Pat', 'Grüße an alle, 100% sicher.'))
        db.session.commit()
        ctx = _owner_ctx(lib, availability={'summaries': False, 'notes': False})
        hit = ia.tool_search_text(ctx, {'query': 'Grüße'})['results']
        assert [(h['title'], h['speaker'], h['time']) for h in hit] == [('Garden notes', 'Pat', '0:10')]
        assert [h['title'] for h in ia.tool_search_text(ctx, {'query': 'need water'})['results']] == ['Garden notes']
        assert sorted(h['title'] for h in ia.tool_search_text(ctx, {'query': '100%'})['results']) == [
            'Garden notes', 'Weekly Sync']


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def test_step_labels_for_the_new_tools():
    assert step_label('search_text', {'query': 'budget freeze'}) == 'Searching for the exact words “budget freeze”'
    assert step_label('search_text', {'query': 'a b', 'match': 'all_words'}) == 'Searching for the words “a b”'
    assert step_label('find_similar_recordings', {}, None, 'Weekly Sync') == 'Finding recordings like Weekly Sync'
    assert step_label('find_similar_recordings', {}) == 'Finding similar recordings'
    assert step_result('search_text', {'result_count': 2, 'results': [{'recording_id': 1}, {'recording_id': 2}]}) == \
        ('done', '2 matches in 2 recordings')
    assert step_result('search_text', {'result_count': 0}) == ('empty', 'No matches')
    assert step_result('find_similar_recordings', {'result_count': 3}) == ('done', '3 recordings')
    assert step_result('find_similar_recordings', {'result_count': 0}) == ('empty', 'No similar recordings')
    assert summary_line({'search_text': 2, 'find_similar_recordings': 1}) == '2 exact searches · 1 similarity search'
    assert step_label('search_text', {'query': 'x'}, 'de') == 'Suche nach dem genauen Wortlaut „x“'
