import {useAsrSpectrogram} from './asrSpectrogram.js';
import {useAsrSegmentTranscription} from './asrSegmentTranscription.js';

export function createAsrTools(state, utils, hooks) {
    return {spectrum:useAsrSpectrogram(state,{...utils,isSelectedSegment:hooks.isSelectedSegment}),
        segmentAsr:useAsrSegmentTranscription(state,{...utils,...hooks})};
}
