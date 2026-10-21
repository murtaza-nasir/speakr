import { describe, it, expect, vi, afterEach } from 'vitest';
import { rangeOverlaps, sampleLink, parseSampleLink, SavedSamplePlayback } from './saved-voice-samples.js';
const id='11111111-1111-4111-8111-111111111111';
describe('saved sample provenance', () => {
    afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();});
    it('keeps independent range across multiple transcript rows without mutation', () => {
        const rows=[{start_time:0,end_time:35},{start_time:35,end_time:72.5},{start_time:72.5,end_time:94},{start_time:94,end_time:110}];
        const before=JSON.stringify(rows), range={start_ms:70000,end_ms:94000};
        expect(rows.map(r=>rangeOverlaps(r,range))).toEqual([false,true,true,false]);
        expect(JSON.stringify(rows)).toBe(before);
    });
    it('links owned UUID identity rather than mutable transcript index', () => {
        const href=sampleLink(2,{id,recording_id:4});
        expect(parseSampleLink(new URL(href,'http://fixture'))).toEqual({sampleId:id,speakerId:2});
        expect(()=>sampleLink(2,{id:'../bad',recording_id:4})).toThrow('access');
        expect(()=>parseSampleLink(new URL('http://fixture/?voice_sample='+id))).toThrow('access');
    });
    it('releases a late preparation lease after another opening takes over', async () => {
        globalThis.document={querySelector:()=>null};
        let resolve; const pending=new Promise(r=>resolve=r);
        const request=vi.fn().mockReturnValueOnce(pending).mockResolvedValue({ok:true,json:async()=>({})});
        const changed=vi.fn(), control=new SavedSamplePlayback(changed,{request});
        const result=control.start(2,id); control.stop();
        resolve({ok:true,json:async()=>({preview_id:id,lease:'a'.repeat(32),state:'preparing'})});
        await result;
        expect(request).toHaveBeenCalledWith(expect.stringContaining('lease='),expect.objectContaining({method:'DELETE'}));
        expect(changed.mock.calls).toHaveLength(1);
        delete globalThis.document;
    });
    it('uses browser fetch without a controller receiver and releases a ready lease on expiry', async () => {
        vi.useFakeTimers();vi.stubGlobal('document',{querySelector:()=>null});
        const expires=Date.now()+1000;
        const ready={preview_id:id,lease:'a'.repeat(32),state:'ready',recording_id:4,start_ms:70000,end_ms:94000,expires_at:expires};
        const calls=[];
        vi.stubGlobal('fetch',function(url,options){
            if(this!==undefined)throw Error('Illegal invocation');
            calls.push({url,options});return Promise.resolve({ok:true,json:async()=>ready});
        });
        const changed=vi.fn(),control=new SavedSamplePlayback(changed);
        expect((await control.start(2,id)).audioUrl).toContain('/audio?lease=');
        await vi.advanceTimersByTimeAsync(1001);
        expect(changed).toHaveBeenLastCalledWith({state:'failed',code:'expired'});
        expect(calls.at(-1).options.method).toBe('DELETE');
    });
    it('polls exact stored identity and rejects changed or malformed ready ranges', async () => {
        vi.stubGlobal('document',{querySelector:()=>null});
        const changed=vi.fn();
        const request=vi.fn().mockResolvedValueOnce({ok:true,json:async()=>({preview_id:id,lease:'b'.repeat(32),state:'preparing'})})
            .mockResolvedValueOnce({ok:false,json:async()=>({code:'changed'})}).mockResolvedValue({ok:true,json:async()=>({})});
        const control=new SavedSamplePlayback(changed,{request,sleep:async()=>{}});
        expect(await control.start(2,id)).toBeNull();
        expect(changed).toHaveBeenLastCalledWith({state:'failed',code:'changed'});
        expect(request.mock.calls.at(-1)[1].method).toBe('DELETE');
    });
});
