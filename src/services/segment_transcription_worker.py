"""Supervised, DB-free ASR of a clipped local segment (JSON stdin/stdout)."""
import json
import math
import os
import signal
import sys
from pathlib import Path


def source_signature(path):
    stat = os.stat(path)
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]


def transcribe_segment(payload):
    from src.services.segment_spectrogram import _run, SpectrogramError
    from src.services.transcription import TranscriptionRequest, get_registry
    path = payload['path']
    try:
        if source_signature(path) != payload['signature']:
            raise SpectrogramError('changed', 409)
        metadata = json.loads(_run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-select_streams', 'a:0', '-show_entries', 'stream=channels,duration:format=duration',
            '-of', 'json', path], 5))
        stream = metadata['streams'][0]
        channels = int(stream['channels'])
        duration = float(stream.get('duration') or metadata['format']['duration'])
        if channels not in (1, 2):
            raise SpectrogramError('channels')
        if not math.isfinite(duration) or payload['end'] > duration + 1e-6:
            raise SpectrogramError('bounds', 400)
        clip = Path(payload['work']) / 'segment.wav'
        _run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1', '-protocol_whitelist', 'file,pipe',
            '-ss', str(payload['start']), '-t', str(payload['end'] - payload['start']), '-i', path,
            '-map', '0:a:0', '-vn', '-ar', '16000', '-ac', str(channels), '-c:a', 'pcm_s16le',
            '-threads', '1', '-y', str(clip)], 15)
        if clip.stat().st_size > 20 * 1024 * 1024:
            raise SpectrogramError('size', 413)
        if source_signature(path) != payload['signature']:
            raise SpectrogramError('changed', 409)
        connector = get_registry().create_connector(payload['connector'], payload['config'])
        params = payload['params']
        with clip.open('rb') as audio:
            result = connector.transcribe(TranscriptionRequest(audio_file=audio, filename='segment.wav',
                mime_type='audio/wav', language=params.get('language'), diarize=False,
                hotwords=params.get('hotwords'), prompt=params.get('initial_prompt'),
                model=params.get('transcription_model')))
        text = (' '.join(segment.text.strip() for segment in result.segments if segment.text.strip())
                if result.segments else result.text.strip())
        if not text:
            raise SpectrogramError('empty')
        if len(text.encode('utf-8')) > 64 * 1024:
            raise SpectrogramError('size', 413)
        if source_signature(path) != payload['signature']:
            raise SpectrogramError('changed', 409)
        return {'text': text}
    except SpectrogramError as error:
        return {'code': error.code}
    except (FileNotFoundError, NotADirectoryError):
        return {'code': 'missing'}
    except Exception:
        # Provider exceptions can include URLs, prompts or secrets. Never return them.
        return {'code': 'provider'}


def main():
    payload = json.load(sys.stdin)
    if os.name != 'nt':
        # The parent uses a new session; this also bounds an orphan's ffmpeg child.
        signal.signal(signal.SIGALRM, lambda *_: os.killpg(os.getpgrp(), signal.SIGKILL))
        signal.setitimer(signal.ITIMER_REAL, float(payload['deadline']))
    try:
        print(json.dumps(transcribe_segment(payload), ensure_ascii=False), flush=True)
    finally:
        if os.name != 'nt':
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    main()
