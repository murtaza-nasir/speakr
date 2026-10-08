import {beforeEach, afterEach, describe, it, expect, vi} from 'vitest';
import {useTranscriptWorkspace, workspaceFingerprint} from './transcriptWorkspace.js';
import {serializeAsrDraft} from './asrWorkplace.js';
const ref=value=>({value});
let state, utils, workspace, sent, context, response, delay;
const segment=(speaker='SPEAKER_00',start=0,end=8)=>({speaker,sentence:'First sentence. Second sentence.',start_time:start,end_time:end,metadata:{keep:true}});
beforeEach(()=>{
    vi.stubGlobal('Vue',{ref,computed:getter=>({get value(){return getter();}}),watch:vi.fn(),nextTick:async()=>{}});
    vi.stubGlobal('document',{querySelector:()=>null});
    vi.stubGlobal('window',{i18n:{t:key=>key}});
    vi.stubGlobal('sessionStorage',{setItem:vi.fn()});
    state=Object.fromEntries(['showAsrEditorModal','editingSegments','speakerMap','availableSpeakers','modalSpeakers','voiceSuggestions','isAutoIdentifying','regenerateSummaryAfterSpeakerUpdate','currentUserName','selectedRecording','recordings'].map(key=>[key,ref(null)]));
    state.showAsrEditorModal.value=true;state.editingSegments.value=[segment()];state.currentUserName.value='Me';
    state.selectedRecording.value={id:9,transcription:serializeAsrDraft(state.editingSegments.value)};state.recordings.value=[state.selectedRecording.value];
    utils={showToast:vi.fn(),setGlobalError:vi.fn()};
    context={voice_labels:['SPEAKER_00'],speaker_label_map:{}};sent=[];response=null;delay=null;
    vi.stubGlobal('fetch',vi.fn(async(url,options)=>{
        if(url.endsWith('workspace_context'))return {ok:true,json:async()=>context};
        if(options?.method==='POST'){
            const body=JSON.parse(options.body);sent.push(body);if(delay)await delay;
            return {ok:true,json:async()=>response||{persistence_status:'saved',recording:{id:9,transcription:JSON.stringify(body.transcript_data)},follow_up:{summary:'skipped'}}};
        }
        return {ok:true,json:async()=>response.recording};
    }));
    const editor={asrEditingLocked:ref(false),asrSelectedIndex:ref(0),selectAsrSegment:vi.fn()};
    const speakers={getSpeakerColor:()=> 'speaker-color-1',loadVoiceSuggestions:vi.fn(),showAutoIdDropdown:ref(false)};
    const modal={selectedSpeaker:ref(null),visibleModalSegments:ref([]),speakerModalData:ref({segments:[]})};
    workspace=useTranscriptWorkspace(state,utils,editor,speakers,modal);
});
afterEach(()=>vi.unstubAllGlobals());
describe('one transcript session',()=>{
    it('includes names and isMe in dirty acknowledgement, excluding UI fields',()=>{
        const a=[{...segment(),id:4,showSuggestions:true}],b=[{...segment(),id:9,showSuggestions:false}];
        expect(workspaceFingerprint(a,{A:{name:'Ana',isMe:false}})).toBe(workspaceFingerprint(b,{A:{name:'Ana',isMe:false}}));
        expect(workspaceFingerprint(a,{A:{name:'Ana',isMe:false}})).not.toBe(workspaceFingerprint(b,{A:{name:'Ana',isMe:true}}));
    });
    it('shares segment objects on mode switch and pins both split parts',async()=>{
        await utils.workspace.start('speakers');const object=state.editingSegments.value[0];
        await utils.workspace.openEditor(0);utils.workspace.back();expect(state.editingSegments.value[0]).toBe(object);
        const parts=[{...object,end_time:4},{...object,start_time:4,speaker:'Boris'}];
        utils.workspace.split(object,parts);state.editingSegments.value=parts;
        expect(workspace.workspacePinned.value).toEqual(parts);
        await utils.workspace.persist(serializeAsrDraft(parts),9,()=>true);
        expect(sent[0].workspace_effects.invalidated_voice_labels).toEqual(['SPEAKER_00']);
        expect(sent[0].workspace_effects.eligible_training_assignments).toEqual({});
    });
    it('keeps source labels through autosave and a second rename',async()=>{
        await utils.workspace.start('speakers');state.speakerMap.value.SPEAKER_00.name='Ana';
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true,true);
        state.speakerMap.value.SPEAKER_00.name='Boris';
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(sent[1].workspace_effects.assignment_changes).toEqual([{current_before:'Ana',final_name:'Boris',source_label:'SPEAKER_00'}]);
        expect(sent[0].regenerate_summary).toBe(false);expect(sent[1].regenerate_summary).toBe(true);
    });
    it('acknowledges canonical spelling and linked ids with matching revision',async()=>{
        await utils.workspace.start();state.speakerMap.value.SPEAKER_00.name='ana';
        response={persistence_status:'saved',recording:{id:9,transcription:JSON.stringify([{...segment('Ana'),speaker_id:11}])}};
        const ack=await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(state.speakerMap.value.SPEAKER_00.name).toBe('Ana');
        expect(JSON.parse(ack.transcription)[0].speaker_id).toBe(11);expect(ack.applyNormalization).toBe(true);
    });
    it('retains names typed while save is in flight',async()=>{
        await utils.workspace.start();state.speakerMap.value.SPEAKER_00.name='ana';
        let release;delay=new Promise(resolve=>release=resolve);
        response={persistence_status:'saved',recording:{id:9,transcription:JSON.stringify([segment('Ana')])}};
        const pending=utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        state.speakerMap.value.SPEAKER_00.name='Boris';release();const ack=await pending;
        expect(state.speakerMap.value.SPEAKER_00.name).toBe('Boris');expect(ack.applyNormalization).toBe(false);
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(sent[1].workspace_effects.assignment_changes[0].current_before).toBe('Ana');
    });
    it('rebases split children when their parent save is acknowledged late',async()=>{
        await utils.workspace.start();state.speakerMap.value.SPEAKER_00.name='Ana';
        let release;delay=new Promise(resolve=>release=resolve);
        const parent=state.editingSegments.value[0];
        const pending=utils.workspace.persist(serializeAsrDraft([parent]),9,()=>true);
        const parts=[{...parent,end_time:4},{...parent,start_time:4,speaker:'Boris'}];
        utils.workspace.split(parent,parts);state.editingSegments.value=parts;
        release();await pending;delay=null;
        await utils.workspace.persist(serializeAsrDraft(parts),9,()=>true);
        expect(sent[1].workspace_effects.assignment_changes.map(c=>c.current_before)).toEqual(['Ana','Ana']);
        expect(sent[1].workspace_effects.invalidated_voice_labels).toEqual(['SPEAKER_00']);
    });
    it('reconciles a lost response through readback without replay',async()=>{
        await utils.workspace.start();state.speakerMap.value.SPEAKER_00.name='Ana';
        response={recording:{id:9,transcription:JSON.stringify([segment('Ana')])}};
        fetch.mockImplementation(async(url,options)=>{if(options?.method==='POST'){sent.push(JSON.parse(options.body));throw Error('lost');}return {ok:true,json:async()=>response.recording};});
        const ack=await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(ack).toBeTruthy();expect(sent).toHaveLength(1);expect(workspace.workspaceFollowUp.value.usage).toBe('unknown');
    });
    it('rebases nested split descendants after a delayed parent acknowledgement',async()=>{
        await utils.workspace.start();state.speakerMap.value.SPEAKER_00.name='Ana';
        let release;delay=new Promise(resolve=>release=resolve);
        const parent=state.editingSegments.value[0];
        const pending=utils.workspace.persist(serializeAsrDraft([parent]),9,()=>true);
        const children=[{...parent,end_time:4},{...parent,start_time:4}];
        utils.workspace.split(parent,children);state.editingSegments.value=children;
        const grandchildren=[{...children[0],end_time:2},{...children[0],start_time:2,speaker:'Boris'}];
        utils.workspace.split(children[0],grandchildren);state.editingSegments.value=[...grandchildren,children[1]];
        release();await pending;delay=null;
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(sent[1].workspace_effects.assignment_changes.map(c=>c.current_before)).toEqual(['Ana','Ana','Ana']);
        expect(sent[1].workspace_effects.invalidated_voice_labels).toEqual(['SPEAKER_00']);
    });
    it.each(['fetch','json'])('ignores a rejected stale context %s after reopen',async(failure)=>{
        let reject;
        fetch.mockImplementationOnce(()=>failure==='fetch' ? new Promise((_,r)=>reject=r) : Promise.resolve({ok:true,json:()=>new Promise((_,r)=>reject=r)}));
        const old=utils.workspace.start();await Promise.resolve();
        utils.workspace.stop();await utils.workspace.start();
        state.editingSegments.value[0].sentence='New unsaved edit';reject(Error('old context failed'));
        expect(await old).toBe(false);expect(state.editingSegments.value[0].sentence).toBe('New unsaved edit');
    });
    it('does not confirm a stale lost-response summary in the new session',async()=>{
        await utils.workspace.start('speakers');state.speakerMap.value.SPEAKER_00.name='Ana';
        let release;
        fetch.mockImplementationOnce(async()=>{throw Error('lost');}).mockImplementationOnce(()=>new Promise(resolve=>release=()=>resolve({ok:true,json:async()=>({transcription:JSON.stringify([segment('Ana')])})})));
        const old=utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>false);
        await Promise.resolve();await Promise.resolve();utils.workspace.stop();await utils.workspace.start('speakers');
        state.speakerMap.value.SPEAKER_00.name='Ana';release();await old;
        expect(utils.workspace.needsSave(false)).toBe(true);expect(workspace.workspaceFollowUp.value).toEqual({});
    });
    it('does not train ambiguous source durations after reopening merged names',async()=>{
        state.editingSegments.value=[segment('Ana')];context={voice_labels:['SPEAKER_00','SPEAKER_01'],speaker_label_map:{SPEAKER_00:'Ana',SPEAKER_01:'Ana'}};
        await utils.workspace.start();state.speakerMap.value.Ana.name='Boris';
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(sent[0].workspace_effects.eligible_training_assignments).toEqual({});
        expect(sent[0].workspace_effects.assignment_changes).toHaveLength(2);
    });
    it('does not acknowledge a failed save with mismatching readback',async()=>{
        await utils.workspace.start();state.speakerMap.value.SPEAKER_00.name='Ana';
        fetch.mockImplementation(async(url,options)=>{if(options?.method==='POST')throw Error('failed');return {ok:true,json:async()=>state.selectedRecording.value};});
        expect(await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true)).toBe(false);
    });
    it('stores incognito draft locally without any backend request',async()=>{
        state.selectedRecording.value.incognito=true;await utils.workspace.start();
        state.editingSegments.value[0].sentence='Local correction';
        const ack=await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(ack).toBeTruthy();expect(fetch).not.toHaveBeenCalled();expect(sessionStorage.setItem).toHaveBeenCalled();
    });
    it('keeps summary pending through autosave and queues it once manually',async()=>{
        await utils.workspace.start('speakers');state.editingSegments.value[0].sentence='Corrected text';
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true,true);
        expect(utils.workspace.needsSave(false)).toBe(true);
        response={persistence_status:'saved',summary_queued:true,recording:{id:9,transcription:serializeAsrDraft(state.editingSegments.value)}};
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(utils.workspace.needsSave(false)).toBe(false);expect(sent.map(b=>b.regenerate_summary)).toEqual([false,true]);
    });
    it('retries only a failed summary without replaying transcript effects',async()=>{
        await utils.workspace.start('speakers');state.speakerMap.value.SPEAKER_00.name='Ana';
        response={persistence_status:'saved',recording:{id:9,transcription:JSON.stringify([segment('Ana')])},follow_up:{summary:'failed',usage:'done'}};
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        fetch.mockImplementationOnce(async(url)=>{expect(url).toBe('/recording/9/generate_summary');return {ok:true,json:async()=>({success:true})};});
        await workspace.retryWorkspaceSummary();
        expect(sent).toHaveLength(1);expect(workspace.workspaceFollowUp.value.summary).toBe('queued');
        expect(utils.workspace.needsSave(false)).toBe(false);
    });
    it('rejects stale LLM results after edit or reopen',async()=>{
        await utils.workspace.start();const token=utils.workspace.llmSnapshot();
        state.speakerMap.value.SPEAKER_00.name='New name';expect(utils.workspace.llmCurrent(token)).toBe(false);
        utils.workspace.stop();await utils.workspace.start();expect(utils.workspace.llmSessionCurrent(token)).toBe(false);
    });
    it('does not resume initialization after closing during context request',async()=>{
        let release;fetch.mockImplementation(()=>new Promise(resolve=>release=()=>resolve({ok:true,json:async()=>context})));
        const pending=utils.workspace.start();utils.workspace.stop();state.showAsrEditorModal.value=false;release();
        expect(await pending).toBe(false);
    });
    it('ignores an old context error after a new session has opened',async()=>{
        let release;fetch.mockImplementationOnce(()=>new Promise(resolve=>release=()=>resolve({ok:false})));
        const old=utils.workspace.start();utils.workspace.stop();await utils.workspace.start();
        state.editingSegments.value[0].sentence='New unsaved edit';release();
        expect(await old).toBe(false);expect(state.editingSegments.value[0].sentence).toBe('New unsaved edit');
        expect(state.showAsrEditorModal.value).toBe(true);
    });
    it('uses each source duration when two labels receive one name',async()=>{
        state.editingSegments.value=[segment('SPEAKER_00',0,1),segment('SPEAKER_01',1,21)];context.voice_labels=['SPEAKER_00','SPEAKER_01'];
        await utils.workspace.start();state.speakerMap.value.SPEAKER_00.name=state.speakerMap.value.SPEAKER_01.name='Ana';
        await utils.workspace.persist(serializeAsrDraft(state.editingSegments.value),9,()=>true);
        expect(sent[0].workspace_effects.training_seconds_by_label).toEqual({SPEAKER_00:1,SPEAKER_01:20});
    });
});
