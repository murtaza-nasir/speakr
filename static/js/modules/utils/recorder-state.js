/**
 * Whether the recorder is in use: recording, paused, or on the recording
 * screen. Work that finishes in the background (an upload of the previous
 * recording, an incognito transcript) must not switch views then, or the
 * user is taken off the recorder mid-recording (#407).
 */
export function recorderInUse(isRecording, currentView) {
    return !!((isRecording && isRecording.value) || (currentView && currentView.value === 'recording'));
}
