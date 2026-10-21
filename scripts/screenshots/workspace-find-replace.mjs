import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';
const base=process.env.SPEAKR_URL || 'http://127.0.0.1:8911';
const out=process.env.SPEAKR_FIND_OUT || 'output/playwright/workspace-find-replace';
assert(['127.0.0.1','localhost'].includes(new URL(base).hostname));
assert(!['8898','8899'].includes(new URL(base).port));
assert((await (await fetch(base+'/__asr-split-ready')).json()).isolated);
await mkdir(out,{recursive:true});
const browser=await chromium.launch({executablePath:process.env.SPEAKR_CHROMIUM_PATH});
const results=[];
try {
 for(const width of [1440,390,360,320]) {
  const context=await browser.newContext({viewport:{width,height:1000},locale:'ru-RU',serviceWorkers:'block',recordVideo:{dir:out}});
  const segments=Array.from({length:100},(_,i)=>({speaker:i===99?'Борис':'Анна',sentence:i===99?'Найденный маркер':'Обычная реплика '+i,start_time:i,end_time:i+.8}));
  const fixture=await(await context.request.post(base+'/__asr-split-fixture',{data:{segments,autosave:false}})).json();
  assert(fixture.isolated);
  const page=await context.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try {
   await page.goto(base,{waitUntil:'domcontentloaded',timeout:120000});
   if(width<737) await page.locator('header button').first().click();
   await page.locator('h4').filter({hasText:fixture.title}).first().click();
   await page.waitForFunction(()=>document.querySelector('#app')._vnode.component.proxy.selectedRecording?.transcription);
   if(width<737) await page.locator('[data-mobile-bottom-nav] button').filter({hasText:'Транскрипция'}).click();
   // Enter through the same speaker workspace used by the ordinary UI.
   if(width<737) { await page.locator('button[title="Редактировать транскрипт"]:visible').first().click(); await page.getByTestId('workspace-back').click(); await page.getByRole('tab',{name:'Спикеры',exact:true}).click(); } else await page.locator('button[title="Определить спикеров"]:visible').click();
   await page.getByTestId('workspace-speaker-Анна').locator('.spk-row-head').click();
   assert.equal(await page.locator('[data-list-index="99"]').count(),0);
   await page.keyboard.press('Control+f');
   const search=page.locator('[data-find-input]:visible');
   await search.fill('Найденный маркер');await search.press('Enter');
   const field=page.getByTestId('asr-segment-text');await field.waitFor();
   assert.equal(await field.inputValue(),'Найденный маркер');
   assert.equal(await page.locator('[data-list-index="99"]').count(),1,'matched speaker must be included in the transcript list');
   if(width>760) assert(await page.locator('[data-list-index="99"]').isVisible());
   assert.equal(await field.evaluate(e=>e.value.slice(e.selectionStart,e.selectionEnd)),'Найденный маркер');
   if(width>760) assert(await page.getByTestId('workspace-transcript').evaluate(e=>e.scrollTop>0),'distant match must scroll into view');
   if(width>760) assert((await page.locator('.find-replace-bar:visible').boundingBox()).height<140,'desktop search controls must retain their compact layout');
   const header=page.locator('[data-testid="asr-editor"] .modal-header');
   const overflow=await header.locator('button:visible').evaluateAll(es=>es.some(e=>{const r=e.getBoundingClientRect();return r.left<0||r.right>innerWidth+1;}));
   assert.equal(overflow,false,'header actions must remain on screen');
   await page.screenshot({path:`${out}/${width}-match.png`});
   assert.deepEqual(errors,[]);results.push({width,status:'PASS'});
  } catch(error) {
   await writeFile(`${out}/${width}-failure.json`,JSON.stringify({errors,state:await page.evaluate(()=>{const a=document.querySelector('#app')._vnode.component.proxy;return {mode:a.workspaceMode,phone:a.workspacePhonePane,selected:a.asrSelectedIndex,locked:a.asrEditingLocked,only:a.onlySelectedSpeaker,visible:a.workspaceVisibleSegments?.map(s=>s.index)};})},null,2));
   await page.screenshot({path:`${out}/${width}-failure.png`});throw error;
  }
  finally {await context.close();}
 }
} finally {await browser.close();await writeFile(out+'/results.json',JSON.stringify(results,null,2));}
console.log(JSON.stringify(results));
