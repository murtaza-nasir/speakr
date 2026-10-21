"""Ordinary read-only fallback keyboard and detected authenticated audio denial."""
import json
import os
from pathlib import Path
import pytest
pytestmark=pytest.mark.skipif(os.environ.get('SPEAKR_SAVED_PLAIN_BROWSER')!='1',reason='owned plain saved sample fixture required')

def test_plain_sample_focus_keyboard_and_audio_denial():
 from playwright.sync_api import sync_playwright,expect
 data=json.loads((Path(os.environ['SPEAKR_MANUAL_FIXTURE_WORKDIR'])/'fixture.json').read_text())
 base=os.environ['SPEAKR_BASE_URL'];out=Path(os.environ['SPEAKR_MANUAL_EVIDENCE']);out.mkdir(parents=True,exist_ok=True)
 errors=[]
 with sync_playwright() as p:
  browser=p.chromium.launch(channel='chromium');context=browser.new_context(viewport={'width':1280,'height':900});page=context.new_page()
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(base+'/login');page.locator('input[name=email]').fill('admin@example.com');page.locator('input[name=password]').fill('changeme')
  page.locator('form:has(input[name=password]) [type=submit]').click();page.wait_for_url(lambda u:'/login' not in u)
  link=f"{base}/recordings/{data['recording_id']}?voice_sample={data['sample_ids'][0]}&voice_speaker={data['speaker_id']}"
  page.goto(link);panel=page.get_by_test_id('saved-range-inspection')
  expect(panel).to_have_count(1,timeout=30000);expect(panel).to_be_visible()
  assert panel.locator('[role=dialog]').evaluate('(e)=>getComputedStyle(e).backgroundColor') not in ('transparent','rgba(0, 0, 0, 0)'), 'Read-only panel must have an opaque surface'
  page.wait_for_function('() => document.activeElement?.closest("[data-testid=saved-range-inspection]")!==null')
  for _ in range(8):
   page.keyboard.press('Tab')
   assert page.evaluate('!!document.activeElement?.closest("[data-testid=saved-range-inspection]")')
  page.keyboard.press('Shift+Tab');assert page.evaluate('!!document.activeElement?.closest("[data-testid=saved-range-inspection]")')
  page.screenshot(path=str(out/'plain-inspection.png'))
  page.keyboard.press('Escape');expect(panel).to_have_count(0)
  page.reload();expect(panel).to_be_visible(timeout=30000)
  page.route('**/manual_voice_samples/*/playbacks/*/audio?*',lambda route:route.fulfill(status=403,content_type='application/json',body='{"code":"access"}'))
  panel.get_by_test_id('saved-range-audio').click(position={'x':25,'y':25})
  expect(panel).to_have_count(0,timeout=15000)
  expect(page.get_by_test_id('saved-range-selection')).to_have_count(0)
  expect(page.get_by_text('Playback unavailable. Check your connection and retry.',exact=True)).to_be_visible()
  page.screenshot(path=str(out/'plain-audio-denied.png'))
  context.close();browser.close()
 assert not errors,errors
 (out/'plain-result.json').write_text(json.dumps({'focus':True,'tab_trap':True,'escape':True,'detected_audio_denial_clears':True,'errors':errors}))
