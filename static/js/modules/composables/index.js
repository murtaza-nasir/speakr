/**
 * Composables module exports
 *
 * Each composable encapsulates related functionality:
 * - recordings: Loading, selecting, filtering recordings
 * - upload: File upload queue management
 * - audio: Microphone/system audio recording
 * - ui: Dark mode, color schemes, sidebar
 * - transcription: Transcription editing (ASR editor, text editor)
 * - speakers: Speaker identification and management
 * - reprocess: Reprocessing transcription/summary
 * - sharing: Public/internal sharing
 * - modals: Modal dialog management
 * - chat: AI chat functionality
 */

export { useRecordings } from './recordings.js';
export { useUpload } from './upload.js';
export { useAudio } from './audio.js';
export { useUI } from './ui.js';
export { useModals } from './modals.js';
export { useSharing } from './sharing.js';
export { useReprocess } from './reprocess.js';
export { useTranscription } from './transcription.js';
export { useSpeakers } from './speakers.js';
export { useChat } from './chat.js';
