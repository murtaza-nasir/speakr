# Voice Tone

Speakr can show how a recording *sounded* next to the transcript: a small emoji chip at the start of lines where something stood out (for example a thank-you or a hesitation), plus a **Tone** toggle to hide them. It is optional. A recording with no tone data looks and behaves exactly as before, and the toggle only appears on recordings that have some.

Speakr does not analyse audio itself. An external scorer of your choice computes tone and sends it to Speakr through the API. Any model works, because the labels, emoji and levels travel with the data.

## What you see

- A chip with an emoji on the first line of each *standout* window. Only windows the scorer marks with `standout` get one.
- Hover or focus (Tab) a chip to see the state, its strength, the top few states in that window, and the time range.
- The **Tone** button (desktop toolbar, mobile header) shows or hides the chips. The choice is remembered per browser.

## Sending tone data

```http
PUT /api/v1/recordings/{id}/tone
```

Requires edit access to the recording. The record is validated before it is stored: a bad payload returns `400` with the reason and never changes what is stored. The limit is 512 KB.

```json
{
  "schema_version": 1,
  "scored_at": "2026-09-29T12:00:00Z",
  "audio_seconds": 312.4,
  "model": {
    "name": "my-scorer",
    "states": {
      "Thankfulness": ["thankful", "🙏", 0.5],
      "Doubt": ["doubtful", "🤔", 0.5]
    }
  },
  "windows": [
    {
      "start": 0.0,
      "end": 7.0,
      "speaker": "SPEAKER_00",
      "scores": { "Thankfulness": 1.4, "Doubt": 0.1 },
      "standout": { "state": "Thankfulness", "label": "thankful", "emoji": "🙏", "strength": "strong" }
    },
    { "start": 7.0, "end": 14.0, "speaker": "SPEAKER_01", "scores": { "Thankfulness": 0.1, "Doubt": 0.1 }, "standout": null }
  ],
  "call": { "windows": 2 }
}
```

| Field | Notes |
|---|---|
| `schema_version` | Must be `1`. |
| `windows` | Required, in time order. Each has `start` and `end` in seconds (`end > start`), `scores` (name to number), and optionally `speaker` and `standout`. |
| `standout` | `label` and `emoji` are required (emoji up to 8 characters, label up to 40). `strength` is shown as text, for example `notable` or `strong`. Use `null` or omit it for windows that should get no chip. |
| `model.states` | Optional. Maps a score name to `[label, emoji, level]`. It fills the popover rows; `level` is the score that counts as notable for that state. Without it the popover shows only the state and range. |
| `call`, `scored_at`, `audio_seconds`, `job`, `source` | Optional and stored as sent. Any other top-level field is rejected. |

Lines find their window by **time**, not by position, so a line you split or merge in the editor still lines up with the right window.

Remove tone data:

```http
DELETE /api/v1/recordings/{id}/tone
```

The recording then has no tone record at all (SQL NULL), and `?include=tone` returns no `tone` field.

## Reading it back

`GET /api/v1/recordings/{id}?include=tone` adds a `tone` field when the recording has tone data. Without `include=tone` the response is unchanged, so existing clients see no difference. Note that `include` replaces the default list, so ask for `include=transcription,summary,notes,tone` if you want those too.

## Example

```bash
curl -X PUT "$SPEAKR/api/v1/recordings/42/tone" \
  -H "Authorization: Bearer $SPEAKR_TOKEN" \
  -H "Content-Type: application/json" \
  --data @tone.json
```

Keep the token on the machine that runs the scorer and out of source control.

## Database

Enabling the feature adds one nullable JSON column, `recording.tone`. It is added on startup the same way other columns are, and running it twice is harmless. Existing rows are untouched.
