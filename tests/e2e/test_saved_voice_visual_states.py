"""Opt-in RU/EN dark/mobile inspection screenshots from the ordinary UI."""
import json
import os
from pathlib import Path
import pytest
pytestmark=pytest.mark.skipif(os.environ.get('SPEAKR_SAVED_BROWSER')!='1',reason='owned saved sample fixture required')

@pytest.mark.parametrize('language,dark,width,height',[('ru',False,1440,1080),('en',True,1440,1080),('ru',True,390,844)])
def test_saved_sample_visual_states(language,dark,width,height):
 from playwright.sync_api import sync_playwright,expect
 data=json.loads((Path(os.environ['SPEAKR_MANUAL_FIXTURE_WORKDIR'])/'fixture.json').read_text())
 base=os.environ['SPEAKR_BASE_URL'];out=Path(os.environ['SPEAKR_MANUAL_EVIDENCE']);out.mkdir(parents=True,exist_ok=True)
 tag=f'{language}-{"dark" if dark else "light"}-{width}'
 errors=[]
 with sync_playwright() as p:
  browser=p.chromium.launch(channel='chromium');context=browser.new_context(viewport={'width':width,'height':height})
  context.add_init_script(f'localStorage.setItem("preferredLanguage",{json.dumps(language)});localStorage.setItem("darkMode",{json.dumps(str(dark).lower())})')
  page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(base+'/login');page.locator('input[name=email]').fill('admin@example.com');page.locator('input[name=password]').fill('changeme')
  page.locator('form:has(input[name=password]) [type=submit]').click();page.wait_for_url(lambda u:'/login' not in u)
  page.goto(base+'/account');page.wait_for_function('() => window.i18n?.isReady')
  page.locator('#tab-speakers').click();page.locator('.view-voice-samples-btn').first.click()
  row=page.get_by_test_id('account-manual-sample-'+data['sample_ids'][1]);expect(row).to_be_visible()
  listen=row.get_by_test_id('sample-listen');expect(listen).to_have_text('Прослушать' if language=='ru' else 'Listen')
  expect(listen).not_to_be_visible()
  row.get_by_test_id('sample-actions').click()
  for testid in ['sample-actions','sample-listen','sample-open-range','sample-detach']:
   bounds=row.get_by_test_id(testid).bounding_box()
   assert bounds['x']>=0 and bounds['x']+bounds['width']<=width
   assert bounds['y']>=0 and bounds['y']+bounds['height']<=height
  page.screenshot(path=str(out/f'account-menu-{tag}.png'),full_page=True)
  listen.click();audio=row.get_by_test_id('sample-audio');expect(audio).to_be_visible(timeout=30000)
  page.wait_for_function('(a) => a.currentTime>0',arg=audio.element_handle())
  expect(listen).not_to_be_visible()
  for testid in ['sample-actions','sample-audio']:
   bounds=row.get_by_test_id(testid).bounding_box()
   assert bounds['x']>=0 and bounds['x']+bounds['width']<=width
  page.screenshot(path=str(out/f'account-{tag}.png'),full_page=True)
  href=row.get_by_test_id('sample-open-range').get_attribute('href');page.goto(base+href)
  page.wait_for_function("() => !!document.querySelector('#app')?.__vue_app__")
  selection=page.get_by_test_id('saved-range-selection');expect(selection).to_have_count(1,timeout=30000);expect(selection).to_be_visible()
  expect(page.get_by_test_id('saved-range-bounds')).to_have_text('00:00:35.000–00:00:55.000')
  assert page.locator('html').evaluate('(e)=>e.classList.contains("dark")')==dark
  page.screenshot(path=str(out/f'source-{tag}.png'),full_page=True)
  assert selection.bounding_box()['width']<=width
  context.close();browser.close()
 assert not errors,errors
