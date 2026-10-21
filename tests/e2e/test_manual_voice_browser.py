"""Ordinary browser range workflow against the owned offline HTTP ASR fixture.

Opt-in only. The worker/PCM/upload/DB are real; the provider's voice vector is
a stand-in, so this does not establish recognition accuracy or physical audio.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import wave

import pytest
from editor_controls import editor_fields

pytestmark = pytest.mark.skipif(os.environ.get('SPEAKR_MANUAL_BROWSER') != '1',
                               reason='requires isolated running manual voice fixture')
BASE = os.environ.get('SPEAKR_BASE_URL', 'http://127.0.0.1:8899').rstrip('/')


def test_ordinary_selected_range_preserves_draft_and_commits_only_clip():
    from playwright.sync_api import sync_playwright, expect
    work = Path(os.environ['SPEAKR_MANUAL_FIXTURE_WORKDIR'])
    fixture = json.loads((work / 'fixture.json').read_text())
    evidence = Path(os.environ.get('SPEAKR_MANUAL_EVIDENCE', '/app/output/playwright/manual-voice-range'))
    evidence.mkdir(parents=True, exist_ok=True)
    before = os.environ.get('SPEAKR_MANUAL_BEFORE') == '1'
    errors, writes, preparations = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chromium')
        context = browser.new_context(viewport={'width': 1440, 'height': 1080}, record_video_dir=str(evidence), locale='en-US')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        def requests(request):
            if request.method == 'POST' and request.url.startswith(BASE):
                writes.append(request.url)
        page.on('request', requests)
        def responses(response):
            if response.url.endswith('/manual_voice_samples/prepare') and response.status == 202:
                preparations.append(response.json()['job_id'])
        page.on('response', responses)
        page.goto(BASE + '/login')
        page.fill('input[name=email]', 'admin@example.com')
        page.fill('input[name=password]', 'changeme')
        page.locator('form:has(input[name=password]) [type=submit]:visible').first.click()
        page.wait_for_url(lambda url: '/login' not in url)
        page.goto(f'{BASE}/recordings/{fixture["recording_id"]}')
        page.locator('button[title="Edit transcript"]:visible').click()
        expect(page.get_by_test_id('asr-editor')).to_be_visible()
        draft_field,start_field,end_field=editor_fields(page,0)
        expect(draft_field).to_be_visible()
        draft_field.fill('Unsaved draft survives manual sample')
        draft = draft_field.input_value()
        boundary = (start_field.input_value(), end_field.input_value())
        if before:
            assert page.get_by_test_id('manual-voice-open').count() == 0
            page.screenshot(path=str(evidence / 'before-workplace.png'))
            page.wait_for_timeout(2000)
        else:
            page.get_by_test_id('manual-voice-open').click()
            panel = page.get_by_test_id('manual-voice-panel')
            expect(panel).to_be_visible()
            page.get_by_test_id('manual-voice-speaker').select_option(str(fixture['speaker_id']))
            page.get_by_test_id('manual-voice-start').fill('00:01:12.500')
            page.get_by_test_id('manual-voice-end').fill('00:01:34.000')
            page.get_by_test_id('manual-voice-listen').click()
            source = page.get_by_test_id('manual-voice-source-audio')
            page.wait_for_function("() => {const a=document.querySelector('[data-testid=manual-voice-source-audio]');return a && !a.paused && a.currentTime>=72.5}")
            page.get_by_test_id('manual-voice-listen').click()
            assert not preparations
            page.screenshot(path=str(evidence / 'after-selected.png'))
            page.get_by_test_id('manual-voice-prepare').click()
            expect(page.get_by_test_id('manual-voice-ready')).to_be_visible(timeout=60000)
            expect(page.get_by_test_id('manual-voice-commit')).to_be_enabled()
            assert page.get_by_test_id('manual-voice-delete').count() == 0
            prepared = page.get_by_test_id('manual-voice-prepared-audio')
            page.screenshot(path=str(evidence / 'after-ready-before-play.png'))
            # Exercise the native control, then inspect the browser media state.
            prepared.click(position={'x': 25, 'y': 25})
            page.wait_for_timeout(1000)
            print('prepared media state', prepared.evaluate('(a)=>({paused:a.paused,currentTime:a.currentTime,duration:a.duration,readyState:a.readyState,networkState:a.networkState,error:a.error?.code,height:a.clientHeight,src:a.currentSrc})'), flush=True)
            page.screenshot(path=str(evidence / 'after-ready-playing.png'))
            page.wait_for_function("() => {const a=document.querySelector('[data-testid=manual-voice-prepared-audio]');return a && !a.paused && a.currentTime>0 && Math.abs(a.duration-21.5)<.001}")
            media_state = prepared.evaluate('(a)=>({duration:a.duration,readyState:a.readyState,currentTime:a.currentTime})')
            page.wait_for_function("() => {const a=document.querySelector('[data-testid=manual-voice-prepared-audio]');return a && a.ended && Math.abs(a.currentTime-21.5)<.001}", timeout=30000)
            audio_url = prepared.get_attribute('src')
            clip = page.request.get(BASE + audio_url)
            assert clip.status == 200 and 'no-store' in clip.headers['cache-control']
            (evidence / 'prepared-range.wav').write_bytes(clip.body())
            page.screenshot(path=str(evidence / 'after-ready.png'))
            page.get_by_test_id('manual-voice-commit').click()
            expect(page.get_by_test_id('manual-voice-committed')).to_be_visible()
            expect(page.get_by_test_id('manual-voice-delete')).to_have_count(1)
            csrf = page.locator('meta[name=csrf-token]').get_attribute('content')
            first_job = preparations[-1]
            duplicate = page.request.post(f'{BASE}/speakers/{fixture["speaker_id"]}/manual_voice_samples',
                data={'job_id': first_job}, headers={'X-CSRFToken': csrf})
            assert duplicate.status == 200 and duplicate.json()['created'] is False
            first_sample = duplicate.json()['id']
            page.get_by_test_id('manual-voice-start').fill('00:00:35.000')
            page.get_by_test_id('manual-voice-end').fill('00:00:55.000')
            page.get_by_test_id('manual-voice-prepare').click()
            expect(page.get_by_test_id('manual-voice-ready')).to_be_visible(timeout=60000)
            page.get_by_test_id('manual-voice-commit').click()
            expect(page.get_by_test_id('manual-voice-delete')).to_have_count(2)
            page.screenshot(path=str(evidence / 'after-two-samples.png'))
            assert not any('/update_trans' in url or '/update_speakers' in url for url in writes)
            page.get_by_test_id('manual-voice-close').click()
            expect(draft_field).to_have_value(draft)
            assert boundary == (start_field.input_value(), end_field.input_value())
            page.get_by_test_id('manual-voice-open').click()
            expect(page.get_by_test_id('manual-voice-delete')).to_have_count(2)
            # Provider refusal is recoverable and never writes a sample.
            (work / 'provider-mode').write_text('multiple')
            page.get_by_test_id('manual-voice-start').fill('00:00:00.000')
            page.get_by_test_id('manual-voice-end').fill('00:00:20.000')
            page.get_by_test_id('manual-voice-prepare').click()
            expect(page.get_by_test_id('manual-voice-error')).to_be_visible(timeout=60000)
            expect(page.get_by_test_id('manual-voice-delete')).to_have_count(2)
            page.screenshot(path=str(evidence / 'after-error.png'))
            (work / 'provider-mode').write_text('slow')
            page.get_by_test_id('manual-voice-prepare').click()
            expect(page.get_by_test_id('manual-voice-preparing')).to_be_visible()
            page.get_by_test_id('manual-voice-end').fill('00:00:21.000')
            page.wait_for_timeout(4000)
            expect(page.get_by_test_id('manual-voice-ready')).to_have_count(0)
            expect(page.get_by_test_id('manual-voice-delete')).to_have_count(2)
            (work / 'provider-mode').write_text('ok')
            # UUID deletion keeps its consumed preparation from resurrecting.
            deleted = page.request.delete(f'{BASE}/speakers/{fixture["speaker_id"]}/manual_voice_samples/{first_sample}',
                                          headers={'X-CSRFToken': csrf})
            assert deleted.status == 200
            retry = page.request.post(f'{BASE}/speakers/{fixture["speaker_id"]}/manual_voice_samples',
                                      data={'job_id': first_job}, headers={'X-CSRFToken': csrf})
            assert retry.status == 410
            page.get_by_test_id('manual-voice-close').click()
            expect(draft_field).to_have_value(draft)
            page.get_by_test_id('manual-voice-open').click()
            expect(page.get_by_test_id('manual-voice-delete')).to_have_count(1)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(evidence / 'after-mobile.png'))
            # Switch through the app's persisted theme used on its ordinary pages.
            page.evaluate("document.documentElement.classList.add('dark');document.documentElement.setAttribute('data-theme','dark')")
            page.screenshot(path=str(evidence / 'after-mobile-dark.png'))
            page.get_by_test_id('manual-voice-close').click()
            with sqlite3.connect(work / 'instance' / 'browser.db') as conn:
                assert conn.execute('SELECT transcription FROM recording WHERE id=?', (fixture['recording_id'],)).fetchone()[0] == fixture['transcription']
                assert conn.execute('SELECT COUNT(*) FROM manual_voice_sample').fetchone()[0] == 1
                assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            original = work / 'uploads' / 'synthetic-selection.wav'
            assert hashlib.sha256(original.read_bytes()).hexdigest() == fixture['audio_sha256']
            with wave.open(str(original)) as source_wav:
                source_pcm = source_wav.readframes(source_wav.getnframes())
            with wave.open(str(evidence / 'prepared-range.wav')) as selected:
                assert selected.getnframes() == 172000 and selected.getnchannels() == 1
                assert selected.readframes(selected.getnframes()) == source_pcm[580000*2:752000*2]
            uploads = [json.loads(line) for line in (work / 'uploads.jsonl').read_text().splitlines()]
            assert [row['duration'] for row in uploads[:2]] == [21.5, 20.0]
            assert all(row['duration'] <= 21.5 for row in uploads)
            assert all(row['query']['return_speaker_embeddings'] == ['true'] for row in uploads)
            (evidence / 'browser-evidence.json').write_text(json.dumps({'uploads': uploads, 'js_errors': errors,
                'draft_preserved': True, 'pcm_exact': True, 'two_ranges': True, 'receipt_retry': True,
                'prepared_media_state': media_state, 'prepared_played_to_end': True,
                'physical_listening': 'not verified', 'provider_accuracy': 'stand-in response only'}, indent=2))
        assert not errors, errors
        video = page.video
        context.close()
        video.save_as(str(evidence / ('before.webm' if before else 'after.webm')))
        browser.close()
