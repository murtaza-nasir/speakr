import { validSegmentRange, spectrogramWindow, plotTime, markerPercent, stripGeometry, viewportWindow,
    scrollWindow, scrollPosition, tileProjection, pngDimensions } from '../utils/asr-spectrogram.js';

const COMPRESSED_LIMIT = 64 * 1024 * 1024;
const DECODED_LIMIT = 16 * 1024 * 1024;
const TILE_LIMIT = 8 * 1024 * 1024;
const FREQUENCY_KEY = 'speakrSpectrogramFrequency';
const FREQUENCIES = ['2000','4000','8000','full'];
const GAIN_KEY = 'speakrSpectrogramGainDb';
const GAINS = Array.from({length:9},(_,i)=>String(i*5));
const errorCodes = ['bounds', 'busy', 'media', 'channels', 'frequency', 'timeout', 'missing', 'remote',
    'changed', 'forbidden', 'expired', 'limit', 'cancelled', 'gain'];

function validatedManifest(m, view, span, gainDb) {
    if (!m || ![m.start,m.end,m.duration,m.segmentStart,m.segmentEnd,m.span,m.maxFrequency].every(Number.isFinite) ||
        m.start < 0 || m.end <= m.start || m.end > m.duration + 1e-8 ||
        m.segmentStart !== view.segmentStart || m.segmentEnd<=m.segmentStart || m.start>m.segmentStart+1e-8 || m.end<m.segmentEnd-1e-8 ||
        Math.abs(m.segmentEnd - Math.min(view.segmentEnd,m.duration)) > 1e-8 ||
        m.span < .25 || m.span > Math.max(60,view.segmentEnd-view.segmentStart) + 1e-8 || Math.abs(m.span-span) > 1e-8 ||
        ![1,2].includes(m.channels) || m.maxFrequency <= 0 || m.maxFrequency > 96000 || m.plotWidth !== 1024 ||
        !Array.isArray(m.tiles) || !m.tiles.length || m.tiles.length > 8192 ||
        (m.gainDb===undefined ? gainDb!==0 : !Number.isInteger(m.gainDb) || m.gainDb!==gainDb)) throw new Error('media');
    let end = m.start;
    m.tiles.forEach((tile,index) => {
        if (tile.index !== index || ![tile.start,tile.end,tile.width,tile.contentStart,tile.contentEnd].every(Number.isFinite) ||
            Math.abs(tile.start-end) > 1e-7 || tile.end <= tile.start || tile.end > m.end + 1e-7 ||
            tile.end-tile.start > Math.min(4*m.span,60) + 1e-7 || !Number.isInteger(tile.width) || tile.width < 1 || tile.width > 4096 ||
            typeof tile.url !== 'string' || !tile.url.startsWith('/') || tile.url.startsWith('//') ||
            tile.contentStart < tile.start-1e-7 || tile.contentEnd > tile.end+1e-7 || tile.contentEnd<tile.contentStart) throw new Error('media');
        end = tile.end;
    });
    if (Math.abs(end-m.end) > 1e-7) throw new Error('media');
    return m;
}

export function useAsrSpectrogram(state, utils) {
    const { editingSegments, selectedRecording, showAsrEditorModal } = state;
    const spectrogram = Vue.ref(null);
    let savedFrequency;
    try { savedFrequency=localStorage.getItem(FREQUENCY_KEY); } catch { /* Browser preferences may be blocked. */ }
    let frequency = FREQUENCIES.includes(savedFrequency) ? savedFrequency : '8000';
    let savedGain;
    try { savedGain=localStorage.getItem(GAIN_KEY); } catch { /* Optional browser preference. */ }
    let gainDb = GAINS.includes(savedGain) ? Number(savedGain) : 0;
    let gainTimer=null;
    const clearGainTimer=()=>{clearTimeout(gainTimer);gainTimer=null;};
    let activeObject = null, requestId = 0, viewportId = 0;
    let openGeneration=0;
    let retryTarget=null;
    let controller = null, tileController = null, pendingItem = null, activeItem = null;
    let viewportElement = null, resizeObserver = null, renewal = null, retryAt = 0, compressedBytes = 0, playEnd = null;
    let programmedScroll = null, suppressClickUntil = 0;
    let lifecycleInstalled=false;
    const leaseReleases=new Set();
    const cache = new Map();
    const audio = () => document.querySelector('[data-testid="asr-editor"] audio, [data-testid="asr-editor"] video');
    const csrf = () => document.querySelector('meta[name="csrf-token"]')?.getAttribute?.('content');
    const jsonOptions = (method, body) => ({method, headers:{'Content-Type':'application/json','X-CSRFToken':csrf()}, body:JSON.stringify(body)});
    const endpoint = (view,id) => `/api/recordings/${view.recordingId}/spectrogram/preparations/${id}`;
    const leasedUrl = (url,item) => `${url}${url.includes('?')?'&':'?'}lease=${encodeURIComponent(item.lease)}`;
    const current = (view,token) => spectrogram.value === view && token === requestId && showAsrEditorModal.value &&
        selectedRecording.value?.id === view.recordingId && editingSegments.value.includes(activeObject) &&
        activeObject.start_time === view.segmentStart && activeObject.end_time === view.segmentEnd;
    const decodedBytes = item => [...item.urls.keys()].reduce((sum,index) => sum + item.manifest.tiles[index].width * (item.manifest.channels === 2 ? 384 : 256) * 4,0);
    const revokeUrls = item => { if (item) {item.urls.forEach(url=>URL.revokeObjectURL(url));item.urls.clear();} };
    const releaseLease = item => {
        if(!item?.lease)return Promise.resolve();
        const lease=item.lease;item.lease=null;item.needsValidation=true;
        const promise=fetch(`${endpoint(item,item.id)}/lease`,jsonOptions('DELETE',{lease})).catch(()=>{});
        leaseReleases.add(promise);promise.finally(()=>leaseReleases.delete(promise));return promise;
    };
    const release = item => {
        if (!item || item.disposed) return;
        item.disposed = true;
        revokeUrls(item);
        item.blobs.forEach(blob=>{compressedBytes-=blob.size;}); item.blobs.clear();
        if (cache.get(item.key) === item) cache.delete(item.key);
        return releaseLease(item);
    };
    const clearPlayback = () => {
        const element = audio();
        if (playEnd !== null) element?.pause();
        element?.removeEventListener('timeupdate',stopAtEnd); element?.removeEventListener('ended',endPlayback); playEnd = null;
    };
    function endPlayback() {clearPlayback();}
    function stopAtEnd() {if (playEnd !== null && audio()?.currentTime >= playEnd) clearPlayback();}
    const cancel = () => {
        requestId++; controller?.abort(); controller=null;
        if (pendingItem && pendingItem !== activeItem) {
            if(cache.get(pendingItem.key)===pendingItem)releaseLease(pendingItem);
            else release(pendingItem);
        }
        pendingItem=null;
    };
    const cancelTiles = () => {viewportId++;tileController?.abort();tileController=null;};
    const fail = (view,error) => {
        view.loading=false; view.error=errorCodes.includes(error.message) ? error.message : 'unavailable';
    };
    const responseJson = async response => {
        const data = await response.json();
        if (!response.ok) {
            if (response.status === 429) retryAt=Date.now()+2000;
            throw new Error(response.status === 410 ? 'expired' : data.code || 'unavailable');
        }
        return data;
    };
    const pausePoll = signal => new Promise((resolve,reject) => {
        const done = () => {signal.removeEventListener('abort',abort);resolve();};
        const timer=setTimeout(done,300);
        const abort=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);reject(new DOMException('Aborted','AbortError'));};
        signal.addEventListener('abort',abort,{once:true});
        if (signal.aborted) abort();
    });
    const startRenewal = () => {
        if (renewal !== null || document.hidden || !spectrogram.value) return;
        renewal=setInterval(async()=> {
            for (const item of new Set([activeItem,pendingItem].filter(Boolean))) {
                if (item.disposed || item.expired || !item.lease) continue;
                const lease=item.lease;
                try {
                    await responseJson(await fetch(`${endpoint(item,item.id)}/lease`,jsonOptions('POST',{lease})));
                    if(item.lease===lease)item.validatedAt=Date.now();
                }
                catch(error) {
                    if (item.disposed || item.lease!==lease) continue;
                    item.expired=true;
                    if (item === activeItem && spectrogram.value) fail(spectrogram.value,error);
                }
            }
        },30000);
    };
    const ensureLease = async item => {
        if(item.expired || !item.lease)throw new Error('expired');
        if(!item.needsValidation && Date.now()-item.validatedAt<90000)return;
        if(item.validationPromise)return item.validationPromise;
        const lease=item.lease;
        item.validationPromise=(async()=>{
            try {
                const data=await responseJson(await fetch(leasedUrl(endpoint(item,item.id),item)));
                if(data.status!=='ready')throw new Error(data.code || 'expired');
                if(item.disposed || item.lease!==lease)throw new DOMException('Aborted','AbortError');
                await responseJson(await fetch(`${endpoint(item,item.id)}/lease`,jsonOptions('POST',{lease})));
                if(item.disposed || item.lease!==lease)throw new DOMException('Aborted','AbortError');
                item.needsValidation=false;item.validatedAt=Date.now();
            } catch(error) {if(error.name!=='AbortError' && !item.disposed && item.lease===lease)item.expired=true;throw error;}
            finally {item.validationPromise=null;}
        })();
        return item.validationPromise;
    };
    const resumeLifecycle = async () => {
        const view=spectrogram.value,item=activeItem;
        for(const candidate of [activeItem,pendingItem].filter(Boolean))candidate.needsValidation=true;
        if(document.hidden) {clearInterval(renewal);renewal=null;return;}
        if(item) {
            try {await ensureLease(item);if(spectrogram.value===view && activeItem===item)startRenewal();}
            catch(error) {if(spectrogram.value===view && error.name!=='AbortError')fail(view,error);}
        } else startRenewal();
    };
    const installLifecycle = () => {
        if(lifecycleInstalled)return;
        document.addEventListener?.('visibilitychange',resumeLifecycle);window.addEventListener?.('pageshow',resumeLifecycle);lifecycleInstalled=true;
    };
    const removeLifecycle = () => {
        document.removeEventListener?.('visibilitychange',resumeLifecycle);window.removeEventListener?.('pageshow',resumeLifecycle);lifecycleInstalled=false;
    };
    const visibleTiles = (item,window) => item.manifest.tiles.filter(tile=>tile.end>window.start+1e-9 && tile.start<window.end-1e-9);
    const trimBlobs = (needed,item,protectedIndices) => {
        while (compressedBytes+needed > COMPRESSED_LIMIT) {
            const victim=[...cache.values()].find(entry=>entry!==activeItem && entry!==pendingItem && entry!==item);
            if (victim) {release(victim);continue;}
            const candidates=[...new Set([...cache.values(),item])]; let removed=false;
            for (const entry of candidates) {
                for (const [index,blob] of entry.blobs) {
                    if (entry.urls.has(index) || (entry===item && protectedIndices.has(index))) continue;
                    entry.blobs.delete(index);compressedBytes-=blob.size;removed=true;break;
                }
                if (removed) break;
            }
            if (!removed) throw new Error('limit');
        }
    };
    const getBlob = async (item,tile,signal,protectedIndices) => {
        await ensureLease(item);
        if(signal.aborted || item.disposed)throw new DOMException('Aborted','AbortError');
        if (item.blobs.has(tile.index)) return item.blobs.get(tile.index);
        const response=await fetch(leasedUrl(tile.url,item),{signal});
        if (!response.ok) {await responseJson(response);return;}
        const blob=await response.blob();
        if (signal.aborted || item.disposed) throw new DOMException('Aborted','AbortError');
        if (blob.type!=='image/png' || blob.size>TILE_LIMIT || !blob.size) throw new Error('media');
        const dimensions=pngDimensions(await blob.slice(0,24).arrayBuffer());
        if(!dimensions || dimensions.width!==tile.width || dimensions.height!==(item.manifest.channels===2?384:256))throw new Error('media');
        if(signal.aborted || item.disposed)throw new DOMException('Aborted','AbortError');
        trimBlobs(blob.size,item,protectedIndices);
        item.blobs.set(tile.index,blob);compressedBytes+=blob.size;
        return blob;
    };
    const syncScroll = () => {
        const view=spectrogram.value;
        if (view?.representation && viewportElement) {
            const left=scrollPosition(view.representation,view.window,view.viewportWidth);
            if (Math.abs(viewportElement.scrollLeft-left)>.25) {programmedScroll=left;viewportElement.scrollLeft=left;}
        }
    };
    const descriptors = () => {
        const view=spectrogram.value;
        if (!view?.representation || !activeItem) return [];
        const offset=view.nativeOffset===null?0:view.nativeOffset-scrollPosition(view.representation,view.window,view.viewportWidth);
        return visibleTiles(activeItem,view.window).filter(tile=>activeItem.urls.has(tile.index)).map(tile=>{
            const projection=tileProjection(tile,view.representation,view.window,view.viewportWidth);
            return {index:tile.index,url:activeItem.urls.get(tile.index),height:view.channels===2?384:256,...projection,left:projection.left+offset};
        });
    };
    const updateDescriptors = () => {
        const view=spectrogram.value;
        if (!view) return;
        view.tiles=descriptors(); view.url=view.tiles[0]?.url || null;
    };
    const displayTiles = async (item,window,signal,isValid) => {
        const tiles=visibleTiles(item,window), height=item.manifest.channels===2?384:256;
        if (tiles.reduce((sum,tile)=>sum+tile.width*height*4,0)>DECODED_LIMIT) throw new Error('limit');
        const protectedIndices=new Set(tiles.map(tile=>tile.index));
        for (const tile of tiles) {await getBlob(item,tile,signal,protectedIndices);if (!isValid()) return false;}
        return true;
    };
    const allocateVisible = async (item,window,isValid=()=>true) => {
        const keep=new Set(visibleTiles(item,window).map(tile=>tile.index));
        // Remove obsolete DOM images before allocating replacements, including a scale swap.
        for (const entry of cache.values()) {
            for (const [index,url] of entry.urls) if (entry!==item || !keep.has(index)) {URL.revokeObjectURL(url);entry.urls.delete(index);}
        }
        updateDescriptors();await utils.nextTick();
        if(item.disposed || !isValid())return false;
        for (const index of keep) if (!item.urls.has(index)) item.urls.set(index,URL.createObjectURL(item.blobs.get(index)));
        if (decodedBytes(item)>DECODED_LIMIT) {revokeUrls(item);throw new Error('limit');}
        return true;
    };
    const loadViewport = async (window={...spectrogram.value?.window}) => {
        const view=spectrogram.value, item=activeItem;
        if (!view?.representation || !item) return;
        cancelTiles(); const token=viewportId; tileController=new AbortController();
        const signal=tileController.signal;
        view.tilesLoading=true;
        const valid=()=>spectrogram.value===view && activeItem===item && token===viewportId && !item.disposed;
        try {
            if (!await displayTiles(item,window,signal,valid) || !valid()) return;
            view.window=window;view.nativeOffset=null;syncScroll();
            if(!await allocateVisible(item,window,valid))return;
            if (valid()) {updateDescriptors();view.tilesLoading=false;}
        } catch(error) {if(valid() && error.name!=='AbortError') {
            item.expired=error.message==='expired';view.tilesLoading=false;view.nativeOffset=null;syncScroll();updateDescriptors();fail(view,error);
        }}
    };
    const load = async (span=spectrogram.value?.span, center=null, explicitRetry=false) => {
        const view=spectrogram.value;
        if (!view) return;
        // Keep failed intent separate from the scale that is still displayed.
        const target={view,segment:activeObject,recording:selectedRecording.value,
            audioPath:selectedRecording.value?.audio_path,frequency,gainDb,span,center};
        retryTarget=target;
        cancel(); const token=requestId;
        // A throttled new intent still supersedes the previous preparation.
        if (Date.now()<retryAt) {view.loading=false;view.error='busy';return;}
        await Promise.allSettled([...leaseReleases]);
        if(!current(view,token))return;
        controller=new AbortController(); const signal=controller.signal;
        const key=`${view.recordingId}:${view.segmentStart}:${view.segmentEnd}:${target.frequency}:${span}:${target.gainDb}`;
        let item=cache.get(key), created=false, replacedItem=null;
        view.loading=true;view.error='';view.requestedSpan=span;
        try {
            if (item?.expired && !explicitRetry) throw new Error('expired');
            if (item && explicitRetry) {
                if(item===activeItem)replacedItem=item;
                else release(item);
                item=null;
            }
            let data;
            if (item) {
                if(item.lease)data=await responseJson(await fetch(leasedUrl(endpoint(view,item.id),item),{signal}));
                else {
                    data=await responseJson(await fetch(`/api/recordings/${view.recordingId}/spectrogram/prepare`,
                        jsonOptions('POST',{start:view.segmentStart,end:view.segmentEnd,frequency:target.frequency,gainDb:target.gainDb,span,existing_id:item.id})));
                    if(!current(view,token) || data.id!==item.id || data.status!=='ready' || typeof data.lease!=='string' || !data.lease) {
                        releaseLease({recordingId:view.recordingId,id:data.id,lease:data.lease});
                        if(!current(view,token))return;
                        throw new Error('expired');
                    }
                    item.lease=data.lease;pendingItem=item;startRenewal();
                }
            }
            else {
                // Read the accepted start response even after closing, to release a late lease by ID.
                data=await responseJson(await fetch(`/api/recordings/${view.recordingId}/spectrogram/prepare`,
                    jsonOptions('POST',{start:view.segmentStart,end:view.segmentEnd,frequency:target.frequency,gainDb:target.gainDb,span})));
                item={key,recordingId:view.recordingId,id:data.id,lease:data.lease,frequency:target.frequency,gainDb:target.gainDb,manifest:null,blobs:new Map(),urls:new Map(),expired:false,disposed:false,
                    needsValidation:false,validatedAt:Date.now(),validationPromise:null};
                created=true;
                if(typeof item.id!=='string' || typeof item.lease!=='string') throw new Error('media');
                if(!current(view,token)) {release(item);return;}
                pendingItem=item;startRenewal();
            }
            const started=Date.now();
            while(['queued','running','preparing'].includes(data.status)) {
                if(!current(view,token)) {if(created)release(item);return;}
                view.progress=data.progress ?? null;
                if(Date.now()-started>300000) throw new Error('timeout');
                await pausePoll(signal);
                data=await responseJson(await fetch(leasedUrl(endpoint(view,item.id),item),{signal}));
            }
            if(data.status!=='ready') throw new Error(data.code || 'cancelled');
            item.manifest=validatedManifest(data.manifest,view,span,target.gainDb);
            item.needsValidation=false;item.validatedAt=Date.now();
            if(!current(view,token)) {if(created)release(item);return;}
            const anchor=center ?? (view.window.start+view.window.end)/2;
            const window=viewportWindow(item.manifest,span,anchor);
            if(!await displayTiles(item,window,signal,()=>current(view,token)) || !current(view,token)) {if(created)release(item);return;}
            cancelTiles();
            if(replacedItem)revokeUrls(replacedItem);
            cache.delete(key);cache.set(key,item);
            spectrogram.value.tiles=[];spectrogram.value.url=null;
            if(!await allocateVisible(item,window,()=>current(view,token))) {if(created)release(item);return;}
            if(!current(view,token)) {if(created)release(item);return;}
            const previousActive=activeItem;
            activeItem=item;pendingItem=null;
            if(previousActive && previousActive!==item && cache.get(previousActive.key)!==previousActive)release(previousActive);
            else if(previousActive && previousActive!==item)releaseLease(previousActive);
            if(replacedItem)release(replacedItem);
            Object.assign(view,{representation:{start:item.manifest.start,end:item.manifest.end},window,span:item.manifest.span,
                duration:item.manifest.duration,channels:item.manifest.channels,maxFrequency:item.manifest.maxFrequency,
                frequency:item.frequency,appliedGainDb:item.gainDb,requestedSpan:item.manifest.span,loading:false,tilesLoading:false,progress:null});
            if(retryTarget===target)retryTarget=null;
            updateDescriptors();
            while(cache.size>4) {const victim=[...cache.values()].find(entry=>entry!==activeItem);if(!victim)break;release(victim);}
            startRenewal();await utils.nextTick();syncScroll();await Promise.allSettled([...leaseReleases]);
        } catch(error) {
            if(current(view,token) && error.name!=='AbortError') {if(item && error.message==='expired')item.expired=true;view.requestedSpan=view.span;fail(view,error);}
            if(created && item!==activeItem)release(item);
            else if(item && item!==activeItem)releaseLease(item);
            if(pendingItem===item)pendingItem=null;
        }
    };
    const closeSpectrogram = async (restoreFocus=false,purgeCache=true,preserveOpening=false) => {
        clearGainTimer();
        retryTarget=null;
        if(!preserveOpening)openGeneration++;
        const segment=activeObject, index=editingSegments.value?.indexOf(segment);
        cancel();cancelTiles();clearPlayback();resizeObserver?.disconnect();resizeObserver=null;viewportElement=null;
        clearInterval(renewal);renewal=null;removeLifecycle();
        programmedScroll=null;suppressClickUntil=0;
        if(purgeCache) {[...new Set([...cache.values(),activeItem].filter(Boolean))].forEach(release);}
        else if(activeItem && cache.get(activeItem.key)!==activeItem)release(activeItem);
        else revokeUrls(activeItem);
        [...cache.values()].forEach(releaseLease);
        activeItem=null;spectrogram.value=null;activeObject=null;retryAt=0;
        if(restoreFocus && index>=0) {
            utils.scrollAsrEditorToIndex?.(index);await utils.nextTick();
            if(utils.isSelectedSegment?.(segment)!==false)document.querySelector('[data-testid="asr-editor"] [data-testid="asr-show-spectrogram"]')?.focus({preventScroll:true});
        }
        await Promise.allSettled([...leaseReleases]);
    };
    const canShowSpectrogram = index => validSegmentRange(editingSegments.value?.[index]) &&
        !!(selectedRecording.value?.audio_ready || selectedRecording.value?.audio_available || selectedRecording.value?.audio_path) &&
        !selectedRecording.value?.audio_deleted_at && !selectedRecording.value?.incognito;
    const openSpectrogram = async index => {
        const generation=++openGeneration;
        if(!canShowSpectrogram(index))return;
        const segment=editingSegments.value[index],recording=selectedRecording.value;
        const validOpening=()=>generation===openGeneration && showAsrEditorModal.value && selectedRecording.value?.id===recording.id && selectedRecording.value?.audio_path===recording.audio_path &&
            editingSegments.value.includes(segment) && utils.isSelectedSegment?.(segment)!==false &&
            canShowSpectrogram(editingSegments.value.indexOf(segment));
        await closeSpectrogram(false,false,true);
        if(!validOpening())return;
        activeObject=segment;
        const start=activeObject.start_time,end=activeObject.end_time,window=spectrogramWindow(start,end);
        spectrogram.value={recordingId:selectedRecording.value.id,segmentStart:start,segmentEnd:end,speaker:activeObject.speaker,
            window,span:Math.min(60,Math.max(.25,end-start)),requestedSpan:Math.min(60,Math.max(.25,end-start)),
            marker:null,markerError:false,channels:1,duration:end,frequency,gainDb,draftGainDb:gainDb,appliedGainDb:null,maxFrequency:8000,url:null,tiles:[],
            viewportWidth:1024,viewportHeight:256,nativeOffset:null,representation:null,error:'',loading:false,tilesLoading:false,progress:null};
        const actualIndex=editingSegments.value.indexOf(segment);
        if(state.asrEditorHighlightIndex)state.asrEditorHighlightIndex.value=actualIndex;
        installLifecycle();await utils.nextTick();
        if(!validOpening()) {
            if(generation===openGeneration && activeObject===segment)await closeSpectrogram();
            return;
        }
        utils.scrollAsrEditorToIndex?.(actualIndex);return load();
    };
    const spectrogramBoundary = segment => {
        const view=spectrogram.value;
        if(!view || activeObject!==segment || view.marker===null)return undefined;
        if(view.recordingId!==selectedRecording.value?.id || segment.start_time!==view.segmentStart || segment.end_time!==view.segmentEnd)return NaN;
        return view.markerError?NaN:view.marker;
    };
    const setSpectrogramFrequency = value => {
        if(!FREQUENCIES.includes(value) || !spectrogram.value || value===frequency)return;
        frequency=value;spectrogram.value.frequency=value;
        try { localStorage.setItem(FREQUENCY_KEY,value); } catch { /* Keep the in-memory choice when storage is unavailable. */ }
        return load();
    };
    const draftSpectrogramGain = value => {
        if(spectrogram.value && GAINS.includes(String(value)))spectrogram.value.draftGainDb=Number(value);
    };
    const commitSpectrogramGain = () => {
        clearGainTimer();const view=spectrogram.value;
        if(!view || !GAINS.includes(String(view.draftGainDb)) || view.draftGainDb===gainDb)return;
        gainDb=view.draftGainDb;view.gainDb=gainDb;
        try {localStorage.setItem(GAIN_KEY,String(gainDb));} catch { /* Keep the committed in-memory level. */ }
        return load(view.span,(view.window.start+view.window.end)/2);
    };
    const queueSpectrogramGain = () => {
        clearGainTimer();const view=spectrogram.value,generation=openGeneration;
        gainTimer=setTimeout(()=>{gainTimer=null;if(spectrogram.value===view && generation===openGeneration)commitSpectrogramGain();},300);
    };
    const resetSpectrogramGain = () => {draftSpectrogramGain(0);return commitSpectrogramGain();};
    const cancelSpectrogramGainDraft = () => {clearGainTimer();if(spectrogram.value)spectrogram.value.draftGainDb=gainDb;};
    const beginSpectrogramGainPointer = () => {clearGainTimer();if(spectrogram.value)spectrogram.value.gainKeyboard=false;};
    const spectrogramFrequencyLabel = () => {
        const view=spectrogram.value;
        if(!view)return '';
        // Label the displayed image, whose actual Nyquist limit can be below the requested range.
        if(view.representation)return `${Number((view.maxFrequency/1000).toFixed(2))} kHz`;
        return view.frequency==='full' ? '…' : `${Number(view.frequency)/1000} kHz`;
    };
    const setSpectrogramMarker = value => {
        const view=spectrogram.value;if(!view)return;
        const time=value===''?NaN:Math.round(Number(value)*100)/100;
        view.marker=time;view.markerError=!Number.isFinite(time) || time<view.segmentStart+.01-1e-8 || time>Math.min(view.segmentEnd-.01,view.duration)+1e-8;
        if(!view.markerError && audio())audio().currentTime=time;
    };
    const resetSpectrogramMarker = () => {if(spectrogram.value){spectrogram.value.marker=null;spectrogram.value.markerError=false;}};
    const clickSpectrogram = event => {
        const view=spectrogram.value;if(!view?.representation || Date.now()<suppressClickUntil)return;
        const rect=event.currentTarget.getBoundingClientRect();
        const scroller=event.currentTarget.querySelector?.('.sw-spectrum-scroll');
        if(scroller && event.clientY!==undefined && event.clientY-scroller.getBoundingClientRect().top>=scroller.clientHeight)return;
        setSpectrogramMarker(plotTime(view.window,event.clientX-rect.left,rect.width));
    };
    const adjustSpectrogramMarker = delta => {
        const view=spectrogram.value;if(view?.representation)setSpectrogramMarker((Number.isFinite(view.marker)?view.marker:(view.window.start+view.window.end)/2)+delta);
    };
    const spectrogramMarkerPercent = () => spectrogram.value?markerPercent(spectrogram.value.window,spectrogram.value.marker):null;
    const spectrogramPlayheadPercent = () => spectrogram.value?markerPercent(spectrogram.value.window,state.modalAudioCurrentTime?.value):null;
    const spectrogramContextPercent = side => {
        const view=spectrogram.value;if(!view)return 0;
        return Math.max(0,Math.min(100,(side==='left'?view.segmentStart-view.window.start:view.window.end-view.segmentEnd)/(view.window.end-view.window.start)*100));
    };
    const scaleReady = () => {
        const view=spectrogram.value;
        return !!(view?.url && view.representation && !view.loading && !view.tilesLoading && view.error!=='expired');
    };
    const fullSpan = () => Math.max(.25,spectrogram.value.representation.end-spectrogram.value.representation.start);
    const canFitSpectrogram = () => {
        if(!scaleReady())return false;
        const view=spectrogram.value;
        return view.window.start>view.representation.start+1e-8 || view.window.end<view.representation.end-1e-8;
    };
    const zoomSpan = zoom => Math.min(fullSpan(),Math.max(.25,spectrogram.value.span*zoom));
    const canZoomSpectrogram = zoom => scaleReady() && Number.isFinite(zoom) && zoom>0 &&
        (zoom>1?canFitSpectrogram():Math.abs(zoomSpan(zoom)-spectrogram.value.span)>1e-8);
    const fitSpectrogram = () => {
        if(!canFitSpectrogram())return;
        const view=spectrogram.value;
        return load(fullSpan(),(view.representation.start+view.representation.end)/2);
    };
    const retrySpectrogram = () => {
        const target=retryTarget,view=spectrogram.value;
        if(target && target.view===view && target.segment===activeObject &&
            target.recording?.id===selectedRecording.value?.id && target.audioPath===selectedRecording.value?.audio_path &&
            current(view,requestId)) {
            frequency=target.frequency;return load(target.span,target.center,true);
        }
        retryTarget=null;return load(view?.span,null,true);
    };
    const navigateSpectrogram = async (zoom,direction=0) => {
        const view=spectrogram.value;if(!view)return;
        const size=view.window.end-view.window.start,center=(view.window.start+view.window.end)/2;
        if(direction) {
            if(!view.representation)return;
            return loadViewport(viewportWindow(view.representation,view.span,center+direction*size/2));
        }
        if(!canZoomSpectrogram(zoom))return;
        const span=zoomSpan(zoom);
        return load(span,Number.isFinite(view.marker)&&!view.markerError?view.marker:center);
    };
    const spectrogramResize = (width,height=viewportElement?.clientHeight) => {
        const view=spectrogram.value;if(!view)return;
        if(Number.isFinite(height) && height>0)view.viewportHeight=height;
        if(!Number.isFinite(width) || width<=0 || view.viewportWidth===width)return;
        view.viewportWidth=width;view.nativeOffset=null;updateDescriptors();utils.nextTick().then(()=>{if(spectrogram.value===view)syncScroll();});
    };
    const spectrogramViewportRef = element => {
        if(element===viewportElement)return;
        resizeObserver?.disconnect();resizeObserver=null;viewportElement=element;
        if(!element)return;
        spectrogramResize(element.clientWidth);syncScroll();
        if(typeof ResizeObserver!=='undefined'){resizeObserver=new ResizeObserver(()=>spectrogramResize(element.clientWidth));resizeObserver.observe(element);}
    };
    const spectrogramScroll = event => {
        const view=spectrogram.value;if(!view?.representation)return;
        const element=event.currentTarget;if(element.clientWidth>0)view.viewportWidth=element.clientWidth;
        if(element.clientHeight>0)view.viewportHeight=element.clientHeight;
        if(programmedScroll!==null && Math.abs(element.scrollLeft-programmedScroll)<=1) {programmedScroll=null;return;}
        programmedScroll=null;
        const next=scrollWindow(view.representation,view.span,element.scrollLeft,view.viewportWidth);
        if(Math.abs(next.start-view.window.start)<1e-9)return;
        suppressClickUntil=Date.now()+150;
        view.nativeOffset=element.scrollLeft;updateDescriptors();return loadViewport(next);
    };
    const spectrogramStripStyle = () => {
        const view=spectrogram.value;if(!view)return {};
        const width=view.representation?stripGeometry(view.representation,view.span,view.viewportWidth).width:view.viewportWidth;
        return {width:`${width}px`,height:`${view.viewportHeight}px`};
    };
    const playSpectrogram = async fromMarker => {
        const view=spectrogram.value,element=audio();
        if(!view || !element || (fromMarker && (view.marker===null || view.markerError)))return;
        clearPlayback();element.currentTime=fromMarker?view.marker:view.segmentStart;playEnd=Math.min(view.segmentEnd,view.duration);
        element.addEventListener('timeupdate',stopAtEnd);element.addEventListener('ended',endPlayback);
        try{await element.play();}catch{clearPlayback();utils.showToast(window.i18n?.t('asrSpectrogram.error_media') || 'Audio unavailable','fa-info-circle');}
    };
    if(Vue.watch) {
        Vue.watch(()=>JSON.stringify([showAsrEditorModal.value,selectedRecording.value?.id,selectedRecording.value?.audio_path,selectedRecording.value?.audio_ready,selectedRecording.value?.audio_deleted_at]),()=>{closeSpectrogram();});
        Vue.watch(()=>[spectrogram.value && activeObject && editingSegments.value.includes(activeObject),activeObject?.start_time,activeObject?.end_time,activeObject?.speaker],()=>{
            if(!activeObject || !spectrogram.value)return;
            if(!editingSegments.value.includes(activeObject)){closeSpectrogram();return;}
            spectrogram.value.speaker=activeObject.speaker;
            if(activeObject.start_time!==spectrogram.value.segmentStart || activeObject.end_time!==spectrogram.value.segmentEnd){
                const index=editingSegments.value.indexOf(activeObject);if(canShowSpectrogram(index))openSpectrogram(index);else closeSpectrogram();
            }
        });
    }
    return {spectrogram,canShowSpectrogram,openSpectrogram,closeSpectrogram,retrySpectrogram,
        cancelSpectrogramPreparation:()=>{clearGainTimer();retryTarget=null;cancel();if(spectrogram.value){spectrogram.value.requestedSpan=spectrogram.value.span;spectrogram.value.loading=false;spectrogram.value.error='cancelled';}},setSpectrogramFrequency,spectrogramFrequencyLabel,
        draftSpectrogramGain,commitSpectrogramGain,queueSpectrogramGain,resetSpectrogramGain,cancelSpectrogramGainDraft,beginSpectrogramGainPointer,
        spectrogramBoundary,setSpectrogramMarker,resetSpectrogramMarker,clickSpectrogram,adjustSpectrogramMarker,
        spectrogramMarkerPercent,spectrogramPlayheadPercent,spectrogramContextPercent,navigateSpectrogram,canZoomSpectrogram,canFitSpectrogram,fitSpectrogram,playSpectrogram,
        releaseSpectrogramPlayback:clearPlayback,spectrogramTiles:()=>spectrogram.value?.tiles || [],spectrogramStripStyle,
        spectrogramScroll,spectrogramViewportRef,spectrogramResize,
        spectrogramResourceUsage:()=>({compressedBytes,decodedBytes:[...cache.values()].reduce((sum,item)=>sum+decodedBytes(item),0),representations:cache.size})};
}
