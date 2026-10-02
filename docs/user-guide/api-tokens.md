# API Tokens

API tokens enable programmatic access to your Speakr instance, allowing you to integrate with automation tools like n8n, Zapier, Make, or custom scripts. Each token is tied to your user account. A token can have full access, the same as logging in through the web interface, or only the scopes you choose.

## Overview

API tokens are personal access tokens that authenticate API requests on your behalf. They're perfect for:

- **Automation workflows** - Trigger transcriptions from n8n, Zapier, or Make
- **Custom scripts** - Build integrations with your existing tools
- **CI/CD pipelines** - Automate audio processing in development workflows
- **Mobile apps** - Access your recordings from custom applications

!!! warning "Security Notice"
    Treat API tokens like passwords. A full-access token can do everything you can do in the web interface. Never share tokens publicly, commit them to version control, or expose them in client-side code.

## Creating a Token

1. Navigate to **Account Settings** → **API Tokens** tab
2. Click **Create Token**
3. Enter a descriptive name (e.g., "n8n automation", "CLI access")
4. Choose an expiration period:
    - **No expiration** - Token remains valid until manually revoked
    - **30 days**, **90 days**, **1 year** - Token automatically expires
5. Choose the access the token needs (see [Scopes](#scopes)):
    - **Read only** - reads recordings and their content
    - **Integration** - read, write and upload
    - **Recorder** - upload only
    - **Full access** - everything, including deleting, sharing and webhooks
    - **Custom** - any set of scopes
6. Click **Create**

!!! important "Save Your Token"
    The token value is only shown once after creation. Copy it immediately and store it securely. If you lose it, you'll need to create a new token.

## Scopes

A token with scopes can call only the API v1 routes those scopes cover. The scope each route needs is listed in the [API Reference](api-reference.md) and in `/api/v1/openapi.json` (`x-required-scopes`).

| Scope | Allows |
|-------|--------|
| `read` | Every GET under `/api/v1` except webhook management: recordings, transcripts, summaries, notes, events, speakers, tags, folders, statistics, audio download |
| `write` | Editing recordings, notes and summaries; adding and removing tags; creating and editing tags, folders and speakers; assigning speakers |
| `upload` | `POST /recordings/upload` and the ASR Voice Recorder upload |
| `process` | Requests that use model or GPU time: transcribe, summarize, regenerate title, identify speakers, chat |
| `share` | Creating, listing and revoking shares |
| `delete` | Deleting recordings, audio, tags, folders and speakers |
| `webhooks` | Everything under `/api/v1/webhooks` |
| `account` | Account settings under `/api/v1/settings` |

- **Existing tokens have full access.** Tokens created before scopes existed, and tokens created through the API without `scopes`, keep full access. The token list shows them as **Full access**.
- **Scoped tokens work only in a header.** A scoped token sent as `?token=` gets `401`. Full tokens still accept the query parameter.
- **Scoped tokens work only on API v1.** Web-interface routes, token management and every route without a declared scope refuse a scoped token with `403`.
- **Scopes can be narrowed, never widened.** `PATCH /api/tokens/{id}` with a smaller `scopes` list removes scopes from a token. A token that needs more scopes needs a new token.
- A request a token is not allowed to make is refused before anything changes, with `403` and the code `insufficient_scope`.

To see the scopes of the token in use, call `GET /api/v1/tokens/current`.

!!! tip "Give each integration its own scoped, expiring token"
    An integration that only reads recordings needs **Read only**. A phone recorder needs **Recorder**. If such a token leaks, it cannot delete recordings, share them or change webhooks.

## Using Your Token

Speakr accepts tokens through multiple methods, giving you flexibility based on your integration needs.

### Authorization Header (Recommended)

The most secure and standard method:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
     https://your-speakr-instance.com/api/v1/recordings
```

### X-API-Token Header

Alternative header format:

```bash
curl -H "X-API-Token: YOUR_TOKEN_HERE" \
     https://your-speakr-instance.com/api/v1/recordings
```

### API-Token Header

Another alternative:

```bash
curl -H "API-Token: YOUR_TOKEN_HERE" \
     https://your-speakr-instance.com/api/v1/recordings
```

### Query Parameter

For simple integrations with a full-access token (less secure - token visible in logs). Scoped tokens do not work in the query string:

```bash
curl "https://your-speakr-instance.com/api/v1/recordings?token=YOUR_TOKEN_HERE"
```

## ASR Voice Recorder Integration

The Android **ASR Voice Recorder** app can automatically send each completed recording to Speakr through its webhook cloud service.

1. Create a token named something descriptive, such as **ASR Voice Recorder**.
2. Choose the **Recorder** access (the `upload` scope only), and an expiration that matches how long the phone will be used.
3. In ASR Voice Recorder, add a webhook destination with this URL:

   ```text
   https://speakr.example.com/api/v1/integrations/asr-voice-recorder/upload
   ```

4. Paste the Speakr token into ASR's **Secret** field.
5. Save the destination. Speakr accepts ASR's authenticated connection test without creating a recording.
6. Record a short test and confirm the recording appears in the token owner's Speakr library.

The integration stores ASR's note as Speakr notes, preserves the supplied recording date, measures the media duration itself, and returns HTTP 200 so ASR marks the connection test or completed delivery successful. Safe retries of the same completed file reuse the existing Speakr recording.

!!! warning "Dedicated token strongly recommended"
    ASR stores the token on the Android device. Use a separate expiring token with only the `upload` scope for this integration. A full-access token on the phone would allow deleting and sharing recordings. Revoke it immediately if the phone is lost, the recorder is uninstalled, or the integration is no longer used. Never place the real token in screenshots, support posts, configuration examples, or source control.

This endpoint is intentionally different from normal API authentication: ASR sends the token in its multipart `secret` field because it cannot add a Bearer or API-token header. Session cookies and normal API headers do not substitute for the Secret field.

## Available API Endpoints

Speakr provides a comprehensive REST API with endpoints for recordings, tags, speakers, processing operations, and more.

!!! tip "Full API Documentation"
    See the complete [API Reference](api-reference.md) for all endpoints, parameters, and examples. You can also access interactive documentation at `/api/v1/docs` on your instance.

**Quick reference of common endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/stats` | GET | Dashboard statistics (gethomepage.dev compatible) |
| `/api/v1/recordings` | GET | List recordings with filtering and pagination |
| `/api/v1/recordings/<id>` | GET | Get recording details |
| `/api/v1/recordings/<id>/transcript` | GET | Get transcript (json, text, srt, vtt) |
| `/api/v1/recordings/<id>/summary` | GET | Get AI-generated summary |
| `/api/v1/recordings/<id>/transcribe` | POST | Queue transcription |
| `/api/v1/recordings/<id>/summarize` | POST | Queue summarization |
| `/api/v1/integrations/asr-voice-recorder/upload` | POST | Receive a completed ASR Voice Recorder file |
| `/api/v1/tags` | GET | List your tags |
| `/api/v1/speakers` | GET | List your speakers |
| `/api/v1/users/me` | GET | Get the current user's profile and group memberships |
| `/api/v1/folders` | GET / POST | List or create folders (requires folders enabled) |
| `/api/v1/transcription` | GET | Discover the active transcription connector and selectable models |
| `/api/v1/webhooks` | GET / POST | List or create event webhooks |
| `/api/v1/tokens/current` | GET | The token in use and its scopes |
| `/api/v1/capabilities` | GET | Features this instance supports |

### Example: List Recordings

```bash
curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
     "https://your-speakr-instance.com/api/v1/recordings?page=1&per_page=25"
```

Response:
```json
{
  "pagination": {
    "page": 1,
    "per_page": 25,
    "total": 42,
    "total_pages": 2,
    "has_next": true,
    "has_prev": false
  },
  "recordings": [
    {
      "id": 123,
      "title": "Team Meeting Notes",
      "status": "COMPLETED",
      "created_at": "Nov 27, 2025, 2:30:00 PM",
      "tags": [...]
    }
  ]
}
```

## Managing Tokens

### Viewing Active Tokens

The API Tokens tab shows all your active tokens with:

- **Name** - The descriptive name you assigned
- **Access** - Full access, or the token's scopes
- **Status** - Active, expired, or revoked
- **Created date** - When the token was created
- **Last used** - When the token was last used for authentication
- **Expiration** - When the token will expire (if set)

### Revoking Tokens

Click the trash icon next to any token to revoke it immediately. Revoked tokens:

- Stop working instantly
- Cannot be restored
- Should be replaced with a new token if needed

!!! tip "Best Practice"
    Revoke tokens you no longer need. If you suspect a token has been compromised, revoke it immediately and create a new one.

## Security Best Practices

### Do's

- ✅ Use descriptive names to track token purposes
- ✅ Set expiration dates for temporary integrations
- ✅ Revoke unused tokens promptly
- ✅ Store tokens in secure credential managers
- ✅ Use environment variables in scripts

### Don'ts

- ❌ Share tokens with others (create separate tokens per user)
- ❌ Commit tokens to version control
- ❌ Include tokens in client-side JavaScript
- ❌ Use the same token for multiple purposes
- ❌ Log full token values in application logs

## Integration Examples

### n8n Workflow

In n8n, use the HTTP Request node with:

- **Authentication**: Header Auth
- **Name**: `Authorization`
- **Value**: `Bearer YOUR_TOKEN_HERE`

### Python Script

```python
import requests

TOKEN = "YOUR_TOKEN_HERE"
BASE_URL = "https://your-speakr-instance.com"

headers = {"Authorization": f"Bearer {TOKEN}"}

# List recordings
response = requests.get(f"{BASE_URL}/api/v1/recordings", headers=headers)
recordings = response.json()["recordings"]

for recording in recordings:
    print(f"{recording['id']}: {recording['title']}")
```

### Shell Script

```bash
#!/bin/bash
TOKEN="YOUR_TOKEN_HERE"
BASE_URL="https://your-speakr-instance.com"

# Get all recordings
curl -s -H "Authorization: Bearer $TOKEN" \
     "$BASE_URL/api/v1/recordings" | jq '.recordings[].title'
```

## Troubleshooting

### Token Not Working

1. **Check token value** - Ensure you copied the complete token without extra spaces
2. **Verify header format** - The Bearer prefix requires a space: `Bearer TOKEN`
3. **Check expiration** - Expired tokens silently fail authentication
4. **Verify endpoint** - Ensure you're using the correct URL

### 401 Unauthorized

- Token may be expired or revoked
- Check the token is being sent correctly
- Verify the endpoint requires authentication

### 403 Forbidden

- Token is valid but you don't have permission for that resource
- Check if the recording belongs to your account
- With `"code": "insufficient_scope"`, the token lacks a scope the route needs. The answer lists `required_scopes` and the token's own `token_scopes`. Create a token with the missing scope.

### 429 Too Many Requests

- The token sent more requests than its limit allows (see [Rate Limits](api-reference.md#rate-limits)). Wait the number of seconds in the `Retry-After` header.

---

Next: Return to [Account Settings](settings.md) →
