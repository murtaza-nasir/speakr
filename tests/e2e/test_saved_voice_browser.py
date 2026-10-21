"""Opt-in ordinary account/cross-tab saved sample flow, isolated synthetic DB."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import wave
import pytest
from editor_controls import editor_fields

pytestmark=pytest.mark.skipif(os.environ.get('SPEAKR_SAVED_BROWSER')!='1',reason='isolated saved sample fixture required')


def test_account_listen_open_range_and_detach_preserves_source():
    from playwright.sync_api import sync_playwright, expect
    work=Path(os.environ['SPEAKR_MANUAL_FIXTURE_WORKDIR'])
    base=os.environ['SPEAKR_BASE_URL'].rstrip('/')
    data=json.loads((work/'fixture.json').read_text())
    evidence=Path(os.environ['SPEAKR_MANUAL_EVIDENCE'])
    evidence.mkdir(parents=True,exist_ok=True)
    errors=[]
    source=work/'uploads/synthetic-selection.wav'
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chromium')
        context=browser.new_context(viewport={'width':1440,'height':1080},record_video_dir=str(evidence),record_video_size={'width':1440,'height':1080})
        page=context.new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(base+'/login')
        page.locator('input[name=email]').fill('admin@example.com')
        page.locator('input[name=password]').fill('changeme')
        page.locator('form:has(input[name=password]) [type=submit]').click()
        page.wait_for_url(lambda u:'/login' not in u)
        page.goto(base+'/account')
        page.locator('#tab-speakers').click()
        page.locator('.view-voice-samples-btn').first.click()
        row=page.get_by_test_id('account-manual-sample-'+data['sample_ids'][0])
        expect(row).to_be_visible()
        more=row.get_by_test_id('sample-actions')
        expect(row.get_by_test_id('sample-listen')).not_to_be_visible()
        more.focus();page.keyboard.press('Enter')
        expect(row.get_by_test_id('sample-listen')).to_be_focused()
        page.keyboard.press('ArrowDown');expect(row.get_by_test_id('sample-open-range')).to_be_focused()
        page.keyboard.press('End');expect(row.get_by_test_id('sample-detach')).to_be_focused()
        page.keyboard.press('Home');expect(row.get_by_test_id('sample-listen')).to_be_focused()
        page.keyboard.press('Escape');expect(more).to_be_focused()
        expect(row.get_by_test_id('sample-listen')).not_to_be_visible()
        page.keyboard.press('Space');expect(row.get_by_test_id('sample-listen')).to_be_focused()
        page.keyboard.press('Tab');expect(row.get_by_test_id('sample-listen')).not_to_be_visible()
        more.focus()
        page.keyboard.press('ArrowUp');expect(row.get_by_test_id('sample-detach')).to_be_focused()
        second=page.get_by_test_id('account-manual-sample-'+data['sample_ids'][1])
        # An open popup may cover the next row; open it via keyboard focus.
        second.get_by_test_id('sample-actions').focus();page.keyboard.press('Enter')
        expect(row.get_by_test_id('sample-listen')).not_to_be_visible()
        expect(second.get_by_test_id('sample-listen')).to_be_visible()
        page.locator('#speakersTabGrid h3').first.click()
        expect(second.get_by_test_id('sample-listen')).not_to_be_visible()
        more.click()
        page.screenshot(path=str(evidence/'account-menu.png'))
        row.get_by_test_id('sample-listen').click()
        expect(row.get_by_test_id('sample-listen')).not_to_be_visible()
        audio=row.get_by_test_id('sample-audio')
        expect(audio).to_be_visible(timeout=30000)
        page.wait_for_function("() => {const a=document.querySelector('[data-testid=sample-audio]');return a&&a.currentTime>0&&Math.abs(a.duration-24)<.001}")
        clip=page.request.get(base+audio.get_attribute('src'))
        assert clip.status==200
        with wave.open(io.BytesIO(clip.body()),'rb') as wav,wave.open(str(source),'rb') as original:
            assert wav.getnframes()==24*8000
            original.setpos(70*8000)
            assert wav.readframes(24*8000)==original.readframes(24*8000)
        (evidence/'preview.wav').write_bytes(clip.body())
        page.screenshot(path=str(evidence/'account-playing.png'))
        more.click()
        with context.expect_page() as new_page:
            row.get_by_test_id('sample-open-range').click()
        target=new_page.value
        target.on('pageerror',lambda e:errors.append(str(e)))
        # The new tab initially contains both server-rendered v-if branches.
        # Wait for Vue to mount before asserting its conditional DOM.
        target.wait_for_function("() => !!document.querySelector('#app')?.__vue_app__")
        selection=target.get_by_test_id('saved-range-selection')
        expect(selection).to_have_count(1,timeout=30000)
        expect(selection).to_be_visible(timeout=30000)
        expect(target.get_by_test_id('saved-range-bounds')).to_have_text('00:01:10.000–00:01:34.000')
        expect(target.locator('[data-sample-range=true]')).to_have_count(2)
        draft_field,start_field,end_field=editor_fields(target)
        assert start_field.input_value()=='35'
        assert end_field.input_value()=='72.5'
        assert draft_field.input_value()=='Independent selection'
        assert audio.evaluate('(a)=>a.paused') is True
        target.screenshot(path=str(evidence/'source-range.png'))
        # Collapse the account after the target acquired its own lease.
        page.locator('.view-voice-samples-btn').first.click()
        target_audio=target.get_by_test_id('saved-range-audio')
        target_audio.click(position={'x':25,'y':25})
        target.wait_for_function("() => {const a=document.querySelector('[data-testid=saved-range-audio]');return a&&!a.paused&&a.currentTime>0}")
        target_audio.evaluate('(a)=>a.pause()')
        target.reload()
        target.wait_for_function("() => !!document.querySelector('#app')?.__vue_app__")
        expect(target.get_by_test_id('saved-range-selection')).to_be_visible(timeout=30000)
        expect(target.locator('[data-sample-range=true]')).to_have_count(2)
        target.get_by_test_id('saved-range-audio').click(position={'x':25,'y':25})
        target.wait_for_function("() => {const a=document.querySelector('[data-testid=saved-range-audio]');return a&&a.ended&&Math.abs(a.duration-24)<.001}",timeout=35000)
        page.locator('.view-voice-samples-btn').first.click()
        expect(row).to_be_visible()
        more.click()
        page.once('dialog',lambda dialog:dialog.dismiss())
        row.get_by_test_id('sample-detach').click()
        expect(row).to_be_visible()
        more.click()
        page.once('dialog',lambda dialog:dialog.accept())
        row.get_by_test_id('sample-detach').click()
        expect(row).to_have_count(0)
        expect(page.locator('[data-testid^=account-manual-sample-]')).to_have_count(1)
        page.reload();page.locator('#tab-speakers').click();page.locator('.view-voice-samples-btn').first.click()
        expect(page.get_by_test_id('account-manual-sample-'+data['sample_ids'][1])).to_be_visible()
        page.screenshot(path=str(evidence/'after-detach.png'))
        videos=[(page.video,'account.webm'),(target.video,'source.webm')]
        context.close()
        for video,name in videos:video.save_as(str(evidence/name))
        browser.close()
    assert not errors,errors
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    db=sqlite3.connect(work/'instance/browser.db')
    assert db.execute('select count(*) from manual_voice_sample').fetchone()[0]==1
    assert db.execute('select transcription from recording where id=?',(data['recording_id'],)).fetchone()[0]==data['transcription']
    assert not (work/'uploads.jsonl').exists(), 'Playback must never contact ASR'
    (evidence/'result.json').write_text(json.dumps({'pcm_exact':True,'native_playback':True,'cross_tab':True,
        'refresh':True,'two_rows_highlighted':True,'detach_one':True,'source_unchanged':True,'no_asr':True,'errors':errors}))


def test_unavailable_source_menu_keeps_detach_and_skips_disabled_items():
    from playwright.sync_api import sync_playwright, expect
    data=json.loads((Path(os.environ['SPEAKR_MANUAL_FIXTURE_WORKDIR'])/'fixture.json').read_text())
    base=os.environ['SPEAKR_BASE_URL'].rstrip('/');evidence=Path(os.environ['SPEAKR_MANUAL_EVIDENCE'])
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chromium');page=browser.new_page(viewport={'width':390,'height':844})
        page.goto(base+'/login');page.locator('input[name=email]').fill('admin@example.com');page.locator('input[name=password]').fill('changeme')
        page.locator('form:has(input[name=password]) [type=submit]').click();page.wait_for_url(lambda u:'/login' not in u)
        def unavailable(route):
            response=route.fetch();body=response.json()
            for sample in body.get('samples',[]): sample['source_available']=False
            route.fulfill(response=response,json=body)
        page.route('**/manual_voice_samples',unavailable)
        page.goto(base+'/account');page.locator('#tab-speakers').click();page.locator('.view-voice-samples-btn').first.click()
        row=page.get_by_test_id('account-manual-sample-'+data['sample_ids'][1])
        row.get_by_test_id('sample-actions').click()
        expect(row.get_by_test_id('sample-listen')).to_be_disabled()
        expect(row.get_by_test_id('sample-open-range')).to_be_disabled()
        expect(row.get_by_test_id('sample-detach')).to_be_focused()
        page.keyboard.press('ArrowDown');expect(row.get_by_test_id('sample-detach')).to_be_focused()
        page.screenshot(path=str(evidence/'account-menu-unavailable.png'),full_page=True)
        page.once('dialog',lambda dialog:dialog.dismiss());page.keyboard.press('Enter')
        expect(row.get_by_test_id('sample-listen')).not_to_be_visible();expect(row).to_be_visible()
        browser.close()
