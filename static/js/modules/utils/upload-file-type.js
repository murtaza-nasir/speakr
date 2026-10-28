/**
 * Which files the upload dialog takes (#510). The file picker's accept list
 * and the check on chosen or dropped files both come from here, so the
 * picker never offers a file the check then skips. The server decides what
 * is media: it probes every upload and refuses one it cannot decode.
 */

// Audio formats, sized against the regular upload limit.
const AUDIO_EXTENSIONS = [
    'mp3', 'm4a', 'wav', 'aac', 'ogg', 'oga', 'opus', 'flac', 'wma', 'aiff', 'caf',
    '3gp', '3gpp', 'amr', 'weba',
];

// Video containers, sized against the audio-only video limit.
const VIDEO_EXTENSIONS = [
    'mp4', 'mov', 'webm', 'mkv', 'avi', 'm4v', 'ts', 'mts', 'wmv', 'flv',
    'mpeg', 'mpg', 'ogv', 'vob', 'asf',
];

const extensionOf = (name) => {
    const dot = (name || '').lastIndexOf('.');
    return dot < 0 ? '' : name.slice(dot + 1).toLowerCase();
};

/** The file input's accept attribute. */
export const UPLOAD_ACCEPT = ['audio/*', 'video/*', 'application/ogg',
    ...[...AUDIO_EXTENSIONS, ...VIDEO_EXTENSIONS].map(ext => `.${ext}`)].join(',');

/** True for a file name with one of the video container extensions. */
export function isVideoFileName(name) {
    return VIDEO_EXTENSIONS.includes(extensionOf(name));
}

/**
 * True when the dialog queues the file: the browser calls it audio or video,
 * or, when the browser gives another or no type, its extension is listed.
 */
export function isAcceptedUploadFile(file) {
    if (!file) return false;
    const type = (file.type || '').toLowerCase();
    if (type.startsWith('audio/') || type.startsWith('video/') || type === 'application/ogg') return true;
    const ext = extensionOf(file.name);
    return AUDIO_EXTENSIONS.includes(ext) || VIDEO_EXTENSIONS.includes(ext);
}
