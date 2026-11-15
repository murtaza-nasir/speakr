/**
 * The public share page (templates/share.html): what a person without an
 * account sees behind a public link.
 *
 * The page is given the recording as JSON in #share-data
 * (src/api/shares.py, view_shared_recording) and shows its transcript the
 * way the recording page does: the same views, speaker colors and window
 * over a long transcript (composables/transcript-view.js), with its find
 * bar and its printing of the whole text. A click on a line plays the audio
 * from there. While the audio plays, the playing line is kept in view until
 * the reader moves the transcript; "Follow" then takes them back to it.
 *
 * Vue comes from the page (vendor/js/vue.global.js), as everywhere in the app.
 */

import { scrollSegmentIntoView, useTranscriptView } from './modules/composables/transcript-view.js';
import { shareTranscript, transcriptText } from './modules/utils/share-transcript.js';
import { createFollow, createPlaybackIndex, isScrollKey } from './modules/utils/follow-playback.js';
import { showToast as toast } from './modules/utils/toast.js';

const { createApp, ref, shallowRef, computed, nextTick, onMounted } = Vue;

// Translations come embedded in the page (includes/i18n_bootstrap.html) for
// the viewer's language: their own interface language when signed in, else
// the browser's Accept-Language. A locale that was not embedded (a saved
// preferredLanguage) is fetched here.
const i18nVersion = ref(0);
if (window.i18n) {
    window.addEventListener('i18nReady', () => { i18nVersion.value++; });
    if (!window.i18n.isReady) window.i18n.init(document.documentElement.lang || 'en');
}

const readData = () => {
    try {
        return JSON.parse(document.getElementById('share-data').textContent) || {};
    } catch (e) {
        console.error('The shared recording could not be read:', e);
        return {};
    }
};

const stored = (key) => {
    try { return localStorage.getItem(key); } catch (e) { return null; }
};
const store = (key, value) => {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
};

/** Put text on the clipboard; without the clipboard API (plain http), through a hidden field. */
const copyText = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (e) { /* fall through */ }
    }
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
    document.body.appendChild(field);
    field.select();
    let done = false;
    try { done = document.execCommand('copy'); } catch (e) { done = false; }
    field.remove();
    return done;
};

const app = createApp({
    setup() {
        // Reading i18nVersion makes the template render again when the
        // translations have loaded.
        function t(key, params) {
            i18nVersion.value;
            return window.i18n ? window.i18n.t(key, params || {}) : key;
        }
        // With an English fallback: this page may render without the bundle.
        function tr(key, fallback) {
            const value = window.i18n && typeof window.i18n.t === 'function' ? window.i18n.t(key) : key;
            return value && value !== key ? value : fallback;
        }

        const { transcript: transcriptData, ...details } = readData();
        const recording = ref(details);
        // The transcript stays out of Vue's reactivity: it never changes,
        // and a long one has tens of thousands of segments.
        const transcript = shareTranscript(transcriptData);
        const processedTranscription = shallowRef(transcript);
        const playbackIndex = createPlaybackIndex(transcript.simpleSegments);

        function updateDocumentTitle() {
            if (!window.i18n || !window.i18n.isReady) return;
            const title = t('sharePage.pageTitle', { title: recording.value.title || '' });
            if (title !== 'sharePage.pageTitle') document.title = title;
        }
        updateDocumentTitle();
        window.addEventListener('i18nReady', updateDocumentTitle);

        const activeTab = ref('transcription');
        const transcriptionViewMode = ref('simple');
        const legendExpanded = ref(false);
        const isDarkMode = ref(document.documentElement.classList.contains('dark'));
        const audioPlayer = ref(null);
        const transcriptBox = ref(null);

        // ---- the transcript ----------------------------------------------------

        // The speaker turns are the recording page's (templates/themes/*/
        // transcript-turn.html, the slot of the visitor's UI theme). What they
        // read beyond the turn itself: the theme, and the timestamps the
        // recording page can show, which this page does not have.
        const uiTheme = ref(document.documentElement.getAttribute('data-ui-theme') || 'default');
        window.addEventListener('speakr:ui-theme', (event) => {
            uiTheme.value = (event.detail && event.detail.id) || document.documentElement.getAttribute('data-ui-theme') || 'default';
        });
        const showTimestampsSimpleView = false;

        const view = useTranscriptView({
            processedTranscription,
            transcriptionViewMode,
            recordingId: () => recording.value.public_id,
            recordingTitle: () => recording.value.title || '',
            relayoutOn: [activeTab, legendExpanded],
            scrollSegmentIntoView,
            t,
            // The page does not scroll, its transcript does: a print of the
            // page would always stop at the first screen.
            print: 'always',
        });

        // ---- the player --------------------------------------------------------

        const audioIsPlaying = ref(false);
        const audioCurrentTime = ref(0);
        // The duration the server measured, when there is one (a browser
        // does not know it for every format, WebM for one)
        const audioDuration = ref(details.audio_duration || 0);
        const audioIsMuted = ref(false);
        const playerVolume = ref(1.0);
        const showSpeedMenu = ref(false);
        const playbackSpeeds = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0];
        const savedRate = parseFloat(stored('playbackRate'));
        const playbackRate = ref(playbackSpeeds.includes(savedRate) ? savedRate : 1.0);

        const audioProgressPercent = computed(() => (audioDuration.value ? (audioCurrentTime.value / audioDuration.value) * 100 : 0));

        function formatAudioTime(seconds) {
            if (!seconds || isNaN(seconds)) return '0:00';
            const hours = Math.floor(seconds / 3600);
            const mins = Math.floor((seconds % 3600) / 60);
            const secs = Math.floor(seconds % 60);
            const tail = `${mins.toString().padStart(hours > 0 ? 2 : 1, '0')}:${secs.toString().padStart(2, '0')}`;
            return hours > 0 ? `${hours}:${tail}` : tail;
        }
        const formatPlaybackRate = (rate) => `${rate}x`;

        // ---- the playing segment, and following it -------------------------------

        const currentPlayingSegmentIndex = ref(-1);
        const follow = createFollow();
        const followPaused = ref(false);

        function showPlaying(time) {
            const index = playbackIndex.at(time);
            currentPlayingSegmentIndex.value = index;
            const reveal = follow.playing(index);
            followPaused.value = follow.paused;
            if (reveal !== null && activeTab.value === 'transcription') view.revealSegment(reveal);
        }
        function stopFollowing() {
            follow.moved();
            followPaused.value = follow.paused;
        }
        function resumeFollowing() {
            const reveal = follow.resume();
            followPaused.value = follow.paused;
            if (reveal !== null) view.revealSegment(reveal);
        }

        function handleAudioPlayPause(event) {
            audioIsPlaying.value = !event.target.paused;
        }
        function handleCustomAudioTimeUpdate(event) {
            audioCurrentTime.value = event.target.currentTime;
            // Before anything was played, no line is the playing one
            if (event.target.currentTime > 0 || !event.target.paused) showPlaying(event.target.currentTime);
        }
        function takeDuration(event) {
            if (audioDuration.value) return;
            const duration = event.target.duration;
            if (duration && isFinite(duration) && duration > 0) audioDuration.value = duration;
        }
        function handleAudioLoadedMetadata(event) {
            takeDuration(event);
            if (playbackRate.value !== 1) event.target.playbackRate = playbackRate.value;
        }
        function handleAudioEnded() {
            audioIsPlaying.value = false;
        }

        function toggleAudioPlayback() {
            const player = audioPlayer.value;
            if (!player) return;
            if (player.paused) player.play();
            else player.pause();
        }
        function toggleAudioMute() {
            const player = audioPlayer.value;
            if (!player) return;
            player.muted = !player.muted;
            audioIsMuted.value = player.muted;
        }
        function setAudioVolume(value) {
            const player = audioPlayer.value;
            if (!player) return;
            playerVolume.value = value;
            player.volume = value;
            if (value > 0 && audioIsMuted.value) {
                player.muted = false;
                audioIsMuted.value = false;
            }
        }
        function seekAudioByPercent(percent) {
            const player = audioPlayer.value;
            if (!player || !audioDuration.value || !isFinite(audioDuration.value)) return;
            player.currentTime = (Math.min(100, Math.max(0, percent)) / 100) * audioDuration.value;
        }
        function setPlaybackRate(rate) {
            playbackRate.value = rate;
            store('playbackRate', rate);
            if (audioPlayer.value) audioPlayer.value.playbackRate = rate;
        }

        // A click on a line plays the audio from there.
        function seekAudioFromEvent(event) {
            const player = audioPlayer.value;
            if (recording.value.audio_deleted_at || !player) return;
            // The end of a text selection is not a click on the line
            const selection = window.getSelection ? window.getSelection() : null;
            if (selection && !selection.isCollapsed && String(selection).trim()) return;
            const line = event.target.closest('[data-start-time]');
            const time = line ? parseFloat(line.dataset.startTime) : NaN;
            if (!isFinite(time)) return;
            player.currentTime = time;
            const played = player.play();
            if (played && played.catch) played.catch(() => {});
            // The reader chose this place: the page follows from here
            follow.resume();
            showPlaying(time);
        }

        // The reader moves the transcript: the wheel, a finger, the keys or
        // the scrollbar. What the page scrolls itself raises none of these.
        onMounted(() => {
            const box = transcriptBox.value;
            if (!box) return;
            box.addEventListener('wheel', stopFollowing, { passive: true });
            box.addEventListener('touchmove', stopFollowing, { passive: true });
            box.addEventListener('keydown', (event) => { if (isScrollKey(event)) stopFollowing(); });
            box.addEventListener('mousedown', (event) => {
                if (event.target === box && event.offsetX >= box.clientWidth) stopFollowing();
            });
        });
        // Going to a match is moving the transcript, too
        const goToTranscriptMatch = (step) => {
            stopFollowing();
            view.goToTranscriptMatch(step);
        };

        // ---- copy ------------------------------------------------------------------

        function animateCopyButton(button) {
            const icon = button ? button.querySelector('i') : null;
            if (!icon) return;
            const original = icon.className;
            icon.className = 'fas fa-check mr-1';
            setTimeout(() => { icon.className = original; }, 2000);
        }
        async function copy(event, text, key, fallback) {
            const button = event ? event.currentTarget : null;
            if (!(await copyText(text || ''))) return;
            animateCopyButton(button);
            toast(tr(key, fallback), 'fa-check-circle', 3000, 'success');
        }
        const copyTranscript = (event) => copy(event, transcriptText(transcript), 'messages.transcriptionCopied', 'Transcript copied');
        const copySummary = (event) => copy(event, recording.value.summary_raw || '', 'messages.summaryCopied', 'Summary copied');
        const copyNotes = (event) => copy(event, recording.value.notes_raw || '', 'messages.notesCopied', 'Notes copied');

        // ---- theme -----------------------------------------------------------------

        function toggleDarkMode() {
            isDarkMode.value = !isDarkMode.value;
            document.documentElement.classList.toggle('dark', isDarkMode.value);
            store('darkMode', String(isDarkMode.value));
            // The color scheme class follows the mode (theme-light-* / theme-dark-*)
            if (typeof window.applyTheme === 'function') window.applyTheme();
            if (window.speakrSaveTheme) window.speakrSaveTheme('darkMode');
            nextTick(view.refreshTranscriptView);
        }

        return {
            t,
            recording, activeTab, transcriptionViewMode, legendExpanded, isDarkMode, toggleDarkMode,
            audioPlayer, transcriptBox,
            processedTranscription, uiTheme, showTimestampsSimpleView, formatTimestamp: formatAudioTime,
            transcriptView: view.transcriptView,
            transcriptFind: view.transcriptFind,
            transcriptFindLabel: view.transcriptFindLabel,
            toggleTranscriptFind: view.toggleTranscriptFind,
            closeTranscriptFind: view.closeTranscriptFind,
            goToTranscriptMatch,
            currentPlayingSegmentIndex, followPaused, resumeFollowing,
            seekAudioFromEvent, copyTranscript, copySummary, copyNotes,
            audioIsPlaying, audioCurrentTime, audioDuration, audioIsMuted, playerVolume, audioProgressPercent,
            formatAudioTime, handleAudioPlayPause, handleCustomAudioTimeUpdate, handleAudioLoadedMetadata,
            handleAudioDurationChange: takeDuration, handleAudioEnded,
            toggleAudioPlayback, toggleAudioMute, setAudioVolume, seekAudioByPercent,
            playbackRate, showSpeedMenu, playbackSpeeds, formatPlaybackRate, setPlaybackRate,
        };
    },
});

app.config.compilerOptions.delimiters = ['${', '}'];
app.mount('#app');

// The loading overlay goes once the page is rendered
nextTick(() => {
    if (window.AppLoader) window.AppLoader.waitForReady();
});
