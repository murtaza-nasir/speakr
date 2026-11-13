#!/usr/bin/env python3
"""Ambrose's find_people tool, name filters that take every name of a person,
and the person and citation rules in the library and recording-chat prompts.

No network, no real LLM.
"""

import json
import os
import secrets
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import Recording, Speaker, User  # noqa: E402
from src.services import inquire_agent as ia  # noqa: E402
from src.services import person_names as pn  # noqa: E402
from src.services.assistant_present import step_label, step_result, summary_line  # noqa: E402
from src.services.recording_chat import (PARTICIPANT_NAME_RULE, WORDS_BEFORE_CITATIONS_RULE,  # noqa: E402
                                         build_chat_messages, participants_with_aliases)

EVERY = {'summaries': True, 'notes': True}
CAP_SURNAMES = ['Abbas', 'Bakr', 'Celik', 'Demir', 'Erdem', 'Farouk', 'Ghani', 'Haddad', 'Iqbal', 'Jaber',
                'Karim', 'Latif']


def _segments(*lines):
    return json.dumps([{'speaker': speaker, 'sentence': text, 'start_time': i * 10.0, 'end_time': i * 10.0 + 8}
                       for i, (speaker, text) in enumerate(lines)])


@pytest.fixture
def people():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f'fp_o_{s}', email=f'fp_o_{s}@local.test', password='x', name='Morgan Lee',
                     timezone='UTC')
        other = User(username=f'fp_x_{s}', email=f'fp_x_{s}@local.test', password='x', timezone='UTC')
        db.session.add_all([owner, other])
        db.session.flush()
        db.session.add_all([
            Speaker(name='Mohammed', user_id=owner.id, email='m.najjar@uni.test',
                    aliases=json.dumps(['Mohammad Najjar'])),
            Speaker(name='José Álvarez', user_id=owner.id, email='jalvarez@corp.test'),
            Speaker(name='Dana Whitfield', user_id=owner.id, aliases=json.dumps(['Dee'])),
            Speaker(name='SPEAKER_03', user_id=owner.id, aliases=json.dumps(['Najjar'])),
            Speaker(name='Najjar Bros', user_id=other.id, email='najjar@else.test',
                    aliases=json.dumps(['Mohammad'])),
        ])
        recs = {
            'ra': Recording(user_id=owner.id, title='Planning', status='COMPLETED',
                            participants='Aziz, Mohammed, Murtaza Nasir', meeting_date=datetime(2026, 10, 1, 15),
                            transcription=_segments(('Aziz', 'Hello.'), ('Mohammed', 'The grant is due.'))),
            'rb': Recording(user_id=owner.id, title='Lab call', status='COMPLETED',
                            participants='Muhammad, SPEAKER_00', meeting_date=datetime(2026, 9, 1, 15),
                            transcription=_segments(('Muhammad', 'Ready.'))),
            'rc': Recording(user_id=owner.id, title='Board', status='COMPLETED',
                            participants='Mohammed (chair), Dana Whitfield', meeting_date=datetime(2026, 8, 1, 15),
                            transcription=_segments(('Dana Whitfield', 'Agreed.'))),
            'rout': Recording(user_id=owner.id, title='Out of scope', status='COMPLETED',
                              participants='Mohammed', meeting_date=datetime(2026, 11, 1, 15),
                              transcription=_segments(('Mohammed', 'Later.'))),
            'rcap': Recording(user_id=owner.id, title='Many Alis', status='COMPLETED',
                              participants=', '.join(f'Ali {n}' for n in CAP_SURNAMES),
                              meeting_date=datetime(2026, 7, 1, 15), transcription=_segments(('Ali Abbas', 'Hi.'))),
            'rx': Recording(user_id=other.id, title='Foreign', status='COMPLETED', participants='Najjar',
                            meeting_date=datetime(2026, 10, 15, 15), transcription=_segments(('Najjar', 'Hi.'))),
        }
        db.session.add_all(recs.values())
        db.session.commit()
        ids = {k: r.id for k, r in recs.items()}
        ids.update(owner=owner.id, other=other.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        Speaker.query.filter(Speaker.user_id.in_([ids['owner'], ids['other']])).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_([ids[k] for k in ('ra', 'rb', 'rc', 'rout', 'rcap', 'rx')])) \
            .delete(synchronize_session=False)
        User.query.filter(User.id.in_([ids['owner'], ids['other']])).delete(synchronize_session=False)
        db.session.commit()


def _ctx(ids, keys=('ra', 'rb', 'rc')):
    return ia.ToolContext(ids['owner'], {}, {ids[k] for k in keys}, EVERY)


def _find(ids, name, keys=('ra', 'rb', 'rc')):
    return ia.tool_find_people(_ctx(ids, keys), {'name': name})


def _by_name(result):
    return {p['name']: p for p in result['people']}


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def test_spelling_variants_share_a_key_and_short_names_do_not():
    keys = {pn.variant_key(t) for t in ('mohammad', 'mohammed', 'muhammad', 'mohamed')}
    assert len(keys) == 1 and None not in keys
    assert pn.variant_key('ahmed') == pn.variant_key('ahmad')
    assert pn.variant_key('fatimah') == pn.variant_key('fatima')
    assert pn.variant_key('joseph') == pn.variant_key('josef')
    assert pn.name_match('Dana', 'Dina') is None          # too short to compare by variant
    assert pn.name_match('Nájjar', 'NAJJAR') == (1, 1, False)
    assert pn.name_match('Najjar', 'm.najjar') == (1, 1, False)
    assert pn.name_match('Najj', 'Najjar') is None         # whole name parts only
    assert pn.name_match('M', 'Mohammed') is None          # an initial alone never matches


# ---------------------------------------------------------------------------
# find_people
# ---------------------------------------------------------------------------

def test_a_surname_finds_the_profile_by_alias_and_email(people):
    with app.app_context():
        res = _find(people, 'Najjar')
        assert [p['name'] for p in res['people']] == ['Mohammed']
        person = res['people'][0]
        assert person['names'] == ['Mohammed', 'Mohammad Najjar']
        assert 'alias' in person['match'] and 'email' in person['match']
        assert person['email'] == 'm.najjar@uni.test' and person['has_profile'] is True
        # Planning and Board; the later recording outside scope is not counted.
        assert person['recording_count'] == 2 and person['latest_recording_date'] == '2026-10-01'
        assert 'speakers' in res['note'] and 'participants' in res['note']


def test_another_users_profiles_and_recordings_are_never_used(people):
    with app.app_context():
        # The whole library the owner can access: the other user's recording is not in it.
        ctx = ia.ToolContext(people['owner'], {}, ia.resolve_allowed_recording_ids(people['owner'], {}), EVERY)
        assert people['rx'] not in ctx.allowed_ids
        res = ia.tool_find_people(ctx, {'name': 'Najjar'})
        assert [p['name'] for p in res['people']] == ['Mohammed']
        assert res['people'][0]['recording_count'] == 3          # Planning, Board and the later one
        assert res['people'][0]['latest_recording_date'] == '2026-11-01'
        other = ia.tool_find_people(ia.ToolContext(people['other'], {}, set(), EVERY), {'name': 'Mohammed'})
        # The other user sees only their own profile, never the owner's "Mohammed".
        assert [p['name'] for p in other['people']] == ['Najjar Bros']
        assert other['people'][0]['recording_count'] == 0


def test_mohammad_mohammed_muhammad_are_one_name(people):
    with app.app_context():
        for query in ('Mohammad', 'mohammed', 'MUHAMMAD', 'Mohamed'):
            found = _by_name(_find(people, query))
            assert set(found) == {'Mohammed', 'Muhammad'}, query
        res = _find(people, 'Mohamed')
        assert all('spelling variant' in p['match'] for p in res['people'])
        exact = _by_name(_find(people, 'Mohammad'))
        assert 'alias' in exact['Mohammed']['match']           # "Mohammad Najjar" has the exact part
        assert 'participant' in exact['Muhammad']['match'] and 'spelling variant' in exact['Muhammad']['match']
        assert 'has_profile' not in exact['Muhammad'] and exact['Muhammad']['recording_count'] == 1


def test_a_full_name_ranks_the_full_match_first(people):
    with app.app_context():
        res = _find(people, 'Mohammad Najjar')
        assert res['people'][0]['name'] == 'Mohammed' and 'partial' not in res['people'][0]['match']
        assert 'partial' in _by_name(res)['Muhammad']['match']


def test_accents_case_and_email_local_part(people):
    with app.app_context():
        assert [p['name'] for p in _find(people, 'jose alvarez')['people']] == ['José Álvarez']
        assert 'profile name' in _find(people, 'ALVAREZ')['people'][0]['match']
        by_email = _find(people, 'jalvarez')['people']
        assert [p['name'] for p in by_email] == ['José Álvarez'] and by_email[0]['match'] == ['email']
        assert by_email[0]['recording_count'] == 0 and by_email[0]['latest_recording_date'] is None


def test_participant_only_and_alias(people):
    with app.app_context():
        aziz = _find(people, 'aziz')['people']
        assert [(p['name'], p['match'], p['recording_count']) for p in aziz] == [('Aziz', ['participant'], 1)]
        dee = _find(people, 'Dee')['people']
        assert [p['name'] for p in dee] == ['Dana Whitfield'] and dee[0]['match'] == ['alias']
        assert dee[0]['names'] == ['Dana Whitfield', 'Dee'] and dee[0]['recording_count'] == 1


def test_generic_speaker_labels_never_match(people):
    with app.app_context():
        assert _find(people, 'SPEAKER_00')['result_count'] == 0
        assert _find(people, 'speaker_03')['people'] == []
        for query in ('Speaker', 'Najjar'):
            names = [n for p in _find(people, query)['people'] for n in p['names']]
            assert not any(n.upper().startswith('SPEAKER') for n in names)


def test_nothing_found_suggests_the_closest_names(people):
    with app.app_context():
        res = _find(people, 'Daana')
        assert res['result_count'] == 0 and res['people'] == []
        assert 'Daana' in res['note'] and 'Dana Whitfield' in res['closest']
        assert 'closest' not in _find(people, 'Xq')
        assert ia.tool_find_people(_ctx(people), {'name': ' '})['error']


def test_results_are_capped(people):
    with app.app_context():
        res = _find(people, 'Ali', keys=('rcap',))
        assert res['result_count'] == ia.FIND_PEOPLE_CAP == len(res['people']) and res['truncated'] is True


# ---------------------------------------------------------------------------
# Filters take every name of a person
# ---------------------------------------------------------------------------

def _titles(result):
    return sorted(r['title'] for r in result['recordings'])


def test_list_recordings_takes_one_name_or_a_list(people):
    with app.app_context():
        ctx = _ctx(people)
        assert _titles(ia.tool_list_recordings(ctx, {'participant': 'Muhammad'})) == ['Lab call']
        assert _titles(ia.tool_list_recordings(ctx, {'participants': ['Mohammed', 'Muhammad']})) == [
            'Board', 'Lab call', 'Planning']
        assert _titles(ia.tool_list_recordings(ctx, {'participants': 'Aziz'})) == ['Planning']
        assert _titles(ia.tool_list_recordings(ctx, {'participant': ['Aziz', 'Dana Whitfield']})) == [
            'Board', 'Planning']


def test_search_text_and_search_transcripts_take_one_name_or_a_list(people, monkeypatch):
    with app.app_context():
        ctx = _ctx(people)
        seen = []
        monkeypatch.setattr(ia, 'basic_text_search_chunks',
                            lambda user_id, query, filters, top_k: seen.append(filters) or [])
        ia.tool_search_transcripts(ctx, {'query': 'grant', 'mode': 'keyword', 'speakers': 'Mohammed'})
        ia.tool_search_transcripts(ctx, {'query': 'grant', 'mode': 'keyword', 'speakers': ['Mohammed', 'Muhammad']})
        assert [f['speaker_names'] for f in seen] == [['Mohammed'], ['Mohammed', 'Muhammad']]
        one = ia.tool_search_text(ctx, {'query': 'Ready', 'speakers': 'Muhammad'})
        assert [h['title'] for h in one['results']] == ['Lab call']
        none = ia.tool_search_text(ctx, {'query': 'Ready', 'speakers': ['Aziz']})
        assert none['result_count'] == 0
        both = ia.tool_search_text(ctx, {'query': 'Ready', 'speakers': ['Aziz', 'Muhammad']})
        assert [h['title'] for h in both['results']] == ['Lab call']


def test_filter_descriptions_ask_for_every_name():
    schemas, executors = ia.build_toolbox(EVERY)
    by_name = {s['function']['name']: s['function'] for s in schemas}
    assert 'find_people' in executors and by_name['find_people']['parameters']['required'] == ['name']
    for tool, prop in (('search_transcripts', 'speakers'), ('search_text', 'speakers'),
                       ('list_recordings', 'participants')):
        description = by_name[tool]['parameters']['properties'][prop]['description']
        assert 'every name find_people returned' in description, tool
    assert 'participant' in by_name['list_recordings']['parameters']['properties']


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

USER_CTX = {'name': 'Morgan Lee', 'title': 'x', 'company': 'y', 'assistant_name': 'Ambrose'}


def test_the_library_prompt_has_the_person_and_citation_rules():
    prompt = ia._system_prompt(USER_CTX, '', EVERY, '')
    assert ia.PEOPLE_TOOL_RULE in prompt and ia.PERSON_NAME_RULE in prompt
    assert '- ' + WORDS_BEFORE_CITATIONS_RULE in prompt
    assert 'call find_people before any exact-word search' in prompt
    assert 'Mohammed in these recordings is Mohammad Najjar, as you said' in prompt
    assert 'never claim the transcript contains it' in prompt
    assert prompt.startswith('You are Ambrose, the AI assistant built into Speakr')
    # Without find_people the rule about the tool is left out, the naming rule stays.
    narrow = ia._system_prompt(USER_CTX, '', EVERY, '', ['search_text'])
    assert 'find_people' not in narrow and ia.PERSON_NAME_RULE in narrow


def test_the_recording_chat_prompt_has_the_rules_and_profile_aliases(people):
    with app.app_context():
        owner = db.session.get(User, people['owner'])
        rec = db.session.get(Recording, people['ra'])
        system = build_chat_messages(rec, owner, 'Who is Najjar?', [])[0][0]['content']
        assert 'Participants and their roles:\nAziz, Mohammed (also: Mohammad Najjar), Murtaza Nasir\n' in system
        assert PARTICIPANT_NAME_RULE in system and WORDS_BEFORE_CITATIONS_RULE in system
        assert 'Mohammed in this meeting is Mohammad Najjar, as you said' in system
        assert 'Ask only when two or more participants fit equally' in system
        with_sources = build_chat_messages(rec, owner, 'Q', [], with_sources=True)[0][0]['content']
        assert PARTICIPANT_NAME_RULE in with_sources and WORDS_BEFORE_CITATIONS_RULE in with_sources
        # Roles stay; another user's profiles are never read.
        assert participants_with_aliases('Mohammed (chair), Dana Whitfield', owner) == \
            'Mohammed (chair) (also: Mohammad Najjar), Dana Whitfield (also: Dee)'
        other = db.session.get(User, people['other'])
        assert participants_with_aliases('Aziz, Mohammed', other) == 'Aziz, Mohammed'
        assert participants_with_aliases(None, owner) is None


def test_the_recording_chat_tools_note_asks_for_find_people():
    scope = {'recording_id': 7}
    tools = ia._recording_tools_block(USER_CTX, EVERY, ['search_text', 'find_people'], '', scope)
    assert ia.PEOPLE_TOOL_RULE in tools
    assert ia.PEOPLE_TOOL_RULE not in ia._recording_tools_block(USER_CTX, EVERY, ['search_text'], '', scope)


def test_find_people_steps_are_labelled():
    assert step_label('find_people', {'name': 'Mohammad'}) == 'Looking up Mohammad'
    assert step_result('find_people', {'result_count': 0, 'people': []}) == ('empty', 'No one by that name')
    assert step_result('find_people', {'result_count': 1}) == ('done', '1 person')
    assert step_result('find_people', {'result_count': 3}) == ('done', '3 people')
    assert summary_line({'find_people': 2, 'search_text': 1}) == '2 person lookups · 1 exact search'
