/**
 * Audio processing and visualization utilities
 */

export function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

export function createAudioVisualizer(canvas, stream, color = '#3b82f6') {
    if (!canvas || !stream) return null;

    const audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = audioContext.createAnalyser();
    const microphone = audioContext.createMediaStreamSource(stream);

    analyser.fftSize = 256;
    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    microphone.connect(analyser);

    const canvasCtx = canvas.getContext('2d');
    const WIDTH = canvas.width;
    const HEIGHT = canvas.height;

    function draw() {
        requestAnimationFrame(draw);
        analyser.getByteFrequencyData(dataArray);

        canvasCtx.fillStyle = 'rgb(17, 24, 39)';
        canvasCtx.fillRect(0, 0, WIDTH, HEIGHT);

        const barWidth = (WIDTH / bufferLength) * 2.5;
        let barHeight;
        let x = 0;

        for (let i = 0; i < bufferLength; i++) {
            barHeight = (dataArray[i] / 255) * HEIGHT;
            canvasCtx.fillStyle = color;
            canvasCtx.fillRect(x, HEIGHT - barHeight, barWidth, barHeight);
            x += barWidth + 1;
        }
    }

    draw();

    return { audioContext, analyser, stop: () => audioContext.close() };
}

export function detectBrowser() {
    const userAgent = navigator.userAgent.toLowerCase();
    if (userAgent.indexOf('firefox') > -1) return 'firefox';
    if (userAgent.indexOf('chrome') > -1 && userAgent.indexOf('edge') === -1) return 'chrome';
    if (userAgent.indexOf('safari') > -1 && userAgent.indexOf('chrome') === -1) return 'safari';
    if (userAgent.indexOf('edge') > -1) return 'edge';
    return 'unknown';
}
