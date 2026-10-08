"""Synthetic ASR fixture, started only by the disposable editor smoke server."""
import io
import json
import os
import time
import wave
from pathlib import Path
from flask import Flask, jsonify, request

if os.environ.get('ASR_SPLIT_SMOKE') != '1' or os.environ.get('ASR_SEGMENT_SMOKE') != '1':
    raise RuntimeError('Disposable segment smoke only')
root = Path('/tmp/speakr-asr-split')
app = Flask(__name__)

@app.post('/asr')
def asr():
    with wave.open(io.BytesIO(request.files['audio_file'].read()), 'rb') as clip:
        capture = dict(duration=clip.getnframes()/clip.getframerate(), channels=clip.getnchannels(), rate=clip.getframerate(), params=dict(request.args))
    (root/'capture.json').write_text(json.dumps(capture))
    mode = (root/'mode.txt').read_text() if (root/'mode.txt').exists() else 'normal'
    if mode == 'delay': time.sleep(6)
    if mode == 'error': return jsonify(error='synthetic private provider error'), 500
    text = '' if mode == 'empty' else 'Повторно распознанный сегмент.'
    return jsonify(text=text, segments=[dict(start=0,end=capture['duration'],text=text)])

app.run(host='127.0.0.1',port=9001,debug=False)
