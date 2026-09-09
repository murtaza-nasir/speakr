# Bundled assets

## voice_canary_v1.wav

A fixed 12 second clip used to detect that the transcription backend's voice
embedding model has changed. See `src/services/voice_embedding_check.py`.

It is synthetic on purpose. Synthesised speech carries no licensing question,
is regenerable from the command below rather than being an opaque blob, and
puts no real person's voice in a public repository, which matters more than
usual for a file whose whole purpose is voice embedding.

Regenerating it produces a DIFFERENT file, and any difference invalidates every
stored reference embedding (the same voice saying the same words, merely
truncated, measures 0.52 against the original). So do not regenerate it in
place. To change the clip, add `voice_canary_v2.wav` and bump
`CANARY_CLIP_VERSION`, which re-baselines every instance on upgrade instead of
warning them all.

```bash
docker run --rm -v "$PWD":/out debian:bookworm-slim bash -c '
apt-get update -qq && apt-get install -y -qq espeak-ng ffmpeg
TEXT="This is a Speakr self test recording. It is used only to check that voice embeddings from the transcription service are still compatible with the voice profiles already stored. Nothing here is transcribed or saved."
espeak-ng -v en-us -s 145 -p 45 -w /tmp/raw.wav "$TEXT"
ffmpeg -i /tmp/raw.wav -ac 1 -ar 16000 -sample_fmt s16 \
  -af "loudnorm=I=-18:TP=-2:LRA=7,apad=pad_dur=0.3" -t 12 /out/voice_canary_v1.wav'
```
