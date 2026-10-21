import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {request} from 'playwright';
const base='http://127.0.0.1:8907',out='output/playwright/asr-workplace-spectrum-refinements';await mkdir(out,{recursive:true});
const c=await request.newContext(),workers=new Set();
try{
 assert((await(await c.get(base+'/__asr-split-ready')).json()).isolated);
 const f=await(await c.post(base+'/__asr-split-fixture',{data:{spectrogram:true,count:2}})).json();
 const html=await(await c.get(base+'/')).text(),csrf=html.match(/name="csrf-token" content="([^"]+)/)[1],headers={'X-CSRFToken':csrf};
 const prepared=await c.post(`${base}/api/recordings/${f.id}/spectrogram/prepare`,{headers,data:{start:0,end:8,frequency:'8000',span:1}});assert.equal(prepared.status(),202);let data=await prepared.json();workers.add(prepared.headers()['x-synthetic-worker']);
 const url=`${base}/api/recordings/${f.id}/spectrogram/preparations/${data.id}`;
 while(data.status!=='ready'){assert(['queued','preparing'].includes(data.status));await new Promise(r=>setTimeout(r,100));const res=await c.get(url+`?lease=${data.lease}`);workers.add(res.headers()['x-synthetic-worker']);data=await res.json();}
 const all=await Promise.all(Array.from({length:30},(_,i)=>c.get(base+data.manifest.tiles[i%data.manifest.tiles.length].url+`?lease=${data.lease}`)));
 for(const res of all){workers.add(res.headers()['x-synthetic-worker']);assert.equal(res.status(),200);assert((await res.body()).subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])));}
 assert.equal(workers.size,2);
 const again=await c.post(`${base}/api/recordings/${f.id}/spectrogram/prepare`,{headers,data:{start:0,end:8,frequency:'8000',span:1,existing_id:data.id}});assert.equal(again.status(),200);assert.equal((await again.json()).id,data.id);
 const result={result:'PASS',workers:[...workers],artifact:data.id,tiles:data.manifest.tiles.length,authenticatedReads:30,cacheOnlyReacquire:true};await writeFile(out+'/two-workers-results.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result));
}finally{await c.dispose();}
