"""DB-free, supervised extraction of a manual voice range. Secrets enter on stdin."""
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

MAX_CLIP_BYTES = 20 * 1024 * 1024


class RangeError(Exception):
    def __init__(self, code):
        self.code = code


def source_digest(path):
    digest, size = hashlib.sha256(), 0
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def crop_pcm(path, work, start_ms, end_ms):
    """Decode before selecting sample indices; no approximate input seeking."""
    work = Path(work)
    decoded, clip = work / 'decoded.wav', work / 'clip.wav'
    try:
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
            '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:a:0',
            '-vn', '-c:a', 'pcm_s16le', '-threads', '1', '-y', str(decoded)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=90)
        with wave.open(str(decoded), 'rb') as source:
            rate, channels, frames = source.getframerate(), source.getnchannels(), source.getnframes()
            if channels not in (1, 2) or rate <= 0:
                raise RangeError('channels')
            if end_ms * rate > frames * 1000:
                raise RangeError('bounds')
            first = start_ms * rate // 1000
            last = (end_ms * rate + 999) // 1000
            if (last - first) * channels * 2 + 44 > MAX_CLIP_BYTES:
                raise RangeError('size')
            source.setpos(first)
            with wave.open(str(clip), 'wb') as output:
                output.setnchannels(channels)
                output.setsampwidth(2)
                output.setframerate(rate)
                remaining = last - first
                while remaining:
                    count = min(remaining, 65536)
                    data = source.readframes(count)
                    if len(data) != count * channels * 2:
                        raise RangeError('media')
                    output.writeframesraw(data)
                    remaining -= count
        return clip, (last - first) / rate
    except (subprocess.SubprocessError, wave.Error, EOFError):
        raise RangeError('media')
    finally:
        decoded.unlink(missing_ok=True)


def validate_response(response, duration, dimension, minimum):
    import numpy as np
    embeddings = response.speaker_embeddings or {}
    if not embeddings:
        raise RangeError('embedding')
    if len(embeddings) > 1:
        raise RangeError('speakers')
    label, raw = next(iter(embeddings.items()))
    if response.speakers and set(response.speakers) != {label}:
        raise RangeError('speakers')
    try:
        vector = np.asarray(raw, dtype=np.float32)
        norm = float(np.linalg.norm(vector.astype(np.float64)))
    except (TypeError, ValueError, OverflowError):
        raise RangeError('embedding')
    if vector.ndim != 1 or len(vector) != dimension or not np.all(np.isfinite(vector)) or not math.isfinite(norm) or norm <= 0:
        raise RangeError('embedding')
    vector = vector / norm
    intervals = []
    for segment in response.segments or []:
        if not segment.text or not segment.text.strip():
            continue
        if segment.speaker != label:
            raise RangeError('speakers')
        start, end = segment.start_time, segment.end_time
        if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float))
                or not isinstance(end, (int, float)) or not math.isfinite(start)
                or not math.isfinite(end) or start < 0 or end <= start):
            raise RangeError('timestamps')
        start, end = max(0, start), min(duration, end)
        if end > start:
            intervals.append((start, end))
    total, previous_end = 0.0, 0.0
    for start, end in sorted(intervals):
        total += max(0, end - max(previous_end, start))
        previous_end = max(previous_end, end)
    if total < minimum:
        raise RangeError('speech')
    return {'vector': vector.tolist(), 'speech_ms': round(total * 1000)}


def prepare_range(payload):
    from src.services.transcription import get_registry, TranscriptionRequest, TranscriptionCapability as Cap
    from src.utils.media_job import source_signature
    try:
        minimum = float(payload['minimum'])
        if not math.isfinite(minimum) or minimum <= 0:
            raise RangeError('unavailable')
        path = payload['path']
        if source_signature(path) != payload['signature']:
            raise RangeError('changed')
        before = source_digest(path)
        clip, duration = crop_pcm(path, payload['work'], payload['start_ms'], payload['end_ms'])
        if source_digest(path) != before:
            raise RangeError('changed')
        connector = get_registry().create_connector(payload['connector'], payload['config'])
        if not all(connector.supports(cap) for cap in (Cap.SPEAKER_EMBEDDINGS, Cap.DIARIZATION, Cap.TIMESTAMPS)):
            raise RangeError('unsupported')
        if payload.get('supervised'):
            # The supervisor rechecks the authoritative registered space after
            # decode, immediately before this explicit clip upload.
            work = Path(payload['work'])
            (work / 'upload.wait').touch(mode=0o600)
            while not (work / 'upload.go').exists():
                time.sleep(.05)
            if source_digest(path) != before:
                raise RangeError('changed')
        params = payload['params']
        count = 1 if connector.supports(Cap.SPEAKER_COUNT_CONTROL) or connector.supports(Cap.EXACT_SPEAKER_COUNT) else None
        with clip.open('rb') as audio:
            response = connector.transcribe(TranscriptionRequest(audio_file=audio, filename='voice-range.wav',
                mime_type='audio/wav', diarize=True, min_speakers=count, max_speakers=count,
                language=params.get('language'), hotwords=params.get('hotwords'),
                prompt=params.get('initial_prompt'), model=params.get('transcription_model')))
        result = validate_response(response, duration, payload['dimension'], minimum)
        if source_digest(path) != before:
            raise RangeError('changed')
        return {**result, 'source_audio_sha256': before[0], 'source_size': before[1]}
    except RangeError as error:
        return {'code': error.code}
    except (FileNotFoundError, NotADirectoryError):
        return {'code': 'missing'}
    except Exception:
        # Provider errors and raw responses may contain secrets; never serialize them.
        return {'code': 'provider'}


def _watchdog(payload):
    from src.utils.media_job import process_identity
    until = time.monotonic() + float(payload['deadline'])
    while time.monotonic() < until:
        if process_identity(payload['parent_pid']) != payload['parent_birth']:
            break
        time.sleep(.5)
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(os.getpid()), '/T', '/F'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
    else:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(1)


def main():
    payload = json.load(sys.stdin)
    payload['supervised'] = True
    threading.Thread(target=_watchdog, args=(payload,), daemon=True).start()
    result = prepare_range(payload)
    output = Path(payload['work']) / 'result.json'
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(result, stream)


if __name__ == '__main__':
    main()
