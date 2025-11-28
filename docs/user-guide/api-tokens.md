# API Tokens

API tokens enable programmatic access to your Speakr instance, allowing you to integrate with automation tools like n8n, Zapier, Make, or custom scripts. Each token is tied to your user account and provides the same access as logging in through the web interface.

## Overview

API tokens are personal access tokens that authenticate API requests on your behalf. They're perfect for:

- **Automation workflows** - Trigger transcriptions from n8n, Zapier, or Make
- **Custom scripts** - Build integrations with your existing tools
- **CI/CD pipelines** - Automate audio processing in development workflows
- **Mobile apps** - Access your recordings from custom applications

!!! warning "Security Notice"
    Treat API tokens like passwords. They provide full access to your account. Never share tokens publicly, commit them to version control, or expose them in client-side code.

## Creating a Token

1. Navigate to **Account Settings** → **API Tokens** tab
2. Click **Create Token**
3. Enter a descriptive name (e.g., "n8n automation", "CLI access")
4. Choose an expiration period:
   - **No expiration** - Token remains valid until manually revoked
   - **30 days**, **90 days**, **1 year** - Token automatically expires
5. Click **Create**

!!! important "Save Your Token"
    The token value is only shown once after creation. Copy it immediately and store it securely. If you lose it, you'll need to create a new token.

## Using Your Token

Speakr accepts tokens through multiple methods, giving you flexibility based on your integration needs.

### Authorization Header (Recommended)

The most secure and standard method:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
     https://your-speakr-instance.com/api/recordings
```

### X-API-Token Header

Alternative header format:

```bash
curl -H "X-API-Token: YOUR_TOKEN_HERE" \
     https://your-speakr-instance.com/api/recordings
```

### API-Token Header

Another alternative:

```bash
curl -H "API-Token: YOUR_TOKEN_HERE" \
     https://your-speakr-instance.com/api/recordings
```

### Query Parameter

For simple integrations (less secure - token visible in logs):

```bash
curl "https://your-speakr-instance.com/api/recordings?token=YOUR_TOKEN_HERE"
```

## Available API Endpoints

Once authenticated, you can access the same data available through the web interface:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/recordings` | GET | List your recordings with pagination |
| `/api/recordings/<id>` | GET | Get a specific recording's details |
| `/api/recordings/<id>/transcript` | GET | Get the transcript text |
| `/api/recordings/<id>/summary` | GET | Get the AI-generated summary |
| `/api/tags` | GET | List your tags |

### Example: List Recordings

```bash
curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
     "https://your-speakr-instance.com/api/recordings?page=1&per_page=25"
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
response = requests.get(f"{BASE_URL}/api/recordings", headers=headers)
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
     "$BASE_URL/api/recordings" | jq '.recordings[].title'
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

---

Next: Return to [Account Settings](settings.md) →
