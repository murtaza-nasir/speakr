---
layout: default
title: Retention & Auto-Deletion
parent: Admin Guide
nav_order: 7
---

# Auto-Deletion and Retention Policies

This document describes the automated retention and deletion system for Speakr recordings.

## Overview

The auto-deletion system provides automated lifecycle management for your recordings, helping you:
- **Comply with data retention policies** - Automatically remove recordings after a specified retention period
- **Manage storage** - Prevent unlimited growth of audio files
- **Maintain critical data** - Keep transcriptions and metadata even after audio deletion
- **Protect important recordings** - Exempt specific recordings from automatic deletion

## Configuration

### Environment Variables

Add these to your `.env` file to configure auto-deletion:

```bash
# Enable or disable the auto-deletion feature
ENABLE_AUTO_DELETION=false  # Set to 'true' to enable

# Global retention period in days (0 = disabled)
GLOBAL_RETENTION_DAYS=90    # Recordings older than this will be processed

# Deletion mode: what to delete
DELETION_MODE=full_recording  # Options: 'audio_only' or 'full_recording'
```

### Deletion Modes

#### Audio-Only Mode (`DELETION_MODE=audio_only`)
- **Deletes**: Audio file only
- **Keeps**: Transcription, summary, notes, metadata
- **Use case**: Long-term record keeping with storage optimization
- **Result**: Recordings appear in "Archived" view, transcription remains searchable

#### Full Recording Mode (`DELETION_MODE=full_recording`)
- **Deletes**: Complete recording including audio, transcription, summary, notes
- **Keeps**: Nothing - recording is permanently removed
- **Use case**: Complete data removal for compliance
- **Result**: Recording is completely removed from the system

## Multi-Tier Retention System

Speakr uses a hierarchical retention policy system:

### 1. Global Retention (System-Wide)
Set via `GLOBAL_RETENTION_DAYS` environment variable. Applies to all recordings unless overridden.

```bash
GLOBAL_RETENTION_DAYS=90  # All recordings older than 90 days
```

### 2. Tag-Based Retention
Tags can override the global retention period with custom retention periods. This is especially powerful with group tags, where group admins can set retention policies for their group's content.

```
Global: 90 days
Tag "Legal Records": 2555 days (7 years)  # Longer retention
Tag "Daily Standups": 14 days  # Shorter retention
Untagged recordings: Uses global (90 days)
```

When a recording has multiple tags with different retention periods, the **shortest** period applies.

### 3. Tag-Based Protection
Individual tags can protect recordings from auto-deletion entirely.

**Example Hierarchy:**
- Global retention: 90 days
- Tag "Sprint Reviews": 180 days (longer than global)
- Tag "Daily Standups": 14 days (shorter than global)
- Tag "Legal" with protection enabled: Never deleted (permanent)

## Protecting Recordings from Deletion

1. Go to **Account Settings** → **Tags** tab
2. Click **Create Tag** or **Edit** an existing tag
3. Enable **"Protect from Auto-Deletion"** checkbox
4. Apply this tag to recordings you want to protect

**When protected:**
- ✅ Recordings with protected tags are exempt from auto-deletion
- ✅ Works regardless of age or retention period
- ✅ Applies to all recordings with that tag

## Archived Recordings

When `DELETION_MODE=audio_only`, recordings become "archived" after audio deletion.

### Accessing Archived Recordings

1. Open the **Recordings** sidebar
2. Click **Advanced Filters**
3. Toggle **"Archived Recordings"** ON

### What You Can Do with Archived Recordings

| Feature | Available | Notes |
|---------|-----------|-------|
| View transcription | ✅ | Full transcript accessible |
| Search content | ✅ | Text search still works |
| Read summary | ✅ | AI summary preserved |
| View/edit notes | ✅ | All metadata accessible |
| Play audio | ❌ | Audio file deleted |
| Re-process | ❌ | Source audio unavailable |
| Share | ✅ | Can share transcription |
| Export | ✅ | Download transcript, summary, notes |

### Archived Recording Indicators

- **Sidebar**: Gray "Archived" badge next to recording title
- **Player**: Info banner: "Audio file has been deleted, but the transcription remains available"
- **Filter**: Separate "Archived" view toggle in advanced filters

## Admin Controls

### Running Auto-Deletion

Auto-deletion runs automatically based on configured schedule. Admins can also trigger manually:

**API Endpoint:**
```bash
POST /admin/auto-deletion/run
```

**Response:**
```json
{
  "checked": 150,
  "deleted_audio_only": 45,
  "deleted_full": 0,
  "exempted": 12,
  "errors": 0
}
```

### Checking Auto-Deletion Stats

**API Endpoint:**
```bash
GET /admin/auto-deletion/stats
```

**Response:**
```json
{
  "enabled": true,
  "global_retention_days": 90,
  "deletion_mode": "audio_only",
  "eligible_count": 45,
  "exempted_count": 12,
  "archived_count": 128
}
```

## Speaker Data Cleanup

When recordings are automatically deleted, Speakr manages associated speaker voice profile data to maintain privacy and GDPR compliance.

### What Gets Cleaned Up

When the auto-deletion job runs, the system automatically:

1. **Removes orphaned speaker profiles**: Speakers with no remaining recordings are deleted entirely
2. **Cleans embedding references**: Recording IDs are removed from speaker voice profile metadata
3. **Applies to both deletion modes**: Works with both `audio_only` and `full_recording` deletion modes

### Cleanup Schedule

Speaker cleanup runs on the same schedule as auto-deletion:

- **Frequency**: Daily at 2:00 AM (server time)
- **Trigger**: Automatically when `ENABLE_AUTO_DELETION=true`
- **No additional configuration**: Uses existing auto-deletion settings

### When Speakers Are Deleted

A speaker is considered "orphaned" and deleted when:

- No `SpeakerSnippet` records exist for the speaker (no voice samples in any recordings)
- No valid recording references remain in the speaker's voice profile metadata
- This occurs after all recordings containing that speaker have been deleted

**Note**: Speakers are preserved as long as they have at least one active recording with speaker identifications.

### Privacy & GDPR Compliance

This automatic cleanup ensures:

- **Data Minimization**: Removes biometric voice data (embeddings) when no longer needed for system function
- **Right to Erasure**: Deletes personal data (voice profiles) when recordings are removed
- **Transparency**: Cleanup activity is logged for audit purposes
- **No Manual Action Required**: Cleanup happens automatically with retention policies

The system treats voice embeddings as biometric data under GDPR. When recordings are deleted (either through auto-deletion or manual deletion), the associated voice profile data is evaluated for removal. This ensures compliance with data protection regulations without requiring manual intervention.

### Monitoring Cleanup Activity

View cleanup statistics in:

- **System logs**: Check application logs for cleanup counts and activity
- **Auto-deletion response**: Speaker cleanup counts included in scheduled job output

Example log entry:
```
INFO - Speaker cleanup completed: 5 speakers deleted, 12 embedding references removed
```

The cleanup process includes these statistics in the auto-deletion job response:

```json
{
  "checked": 123,
  "deleted_audio_only": 45,
  "deleted_full": 0,
  "exempted": 12,
  "speakers_deleted": 5,
  "embeddings_cleaned": 12,
  "speakers_evaluated": 94
}
```

### What Data Is Retained

The system preserves:

- **Active speakers**: Speakers with at least one recording containing their voice
- **Speaker names**: Names are retained as long as associated recordings exist
- **Voice profiles**: Embedding data is kept when recordings reference the speaker

### What Data Is Removed

The system removes:

- **Orphaned voice embeddings**: Biometric voice data for speakers with no recordings
- **Speaker records**: Entire speaker profile when completely orphaned
- **Invalid references**: Recording IDs in embedding history that point to deleted recordings
- **Usage statistics**: Use counts and timestamps for deleted speakers

This ensures that biometric data is only retained when there's a legitimate purpose (active recordings), fulfilling GDPR's data minimization requirement.

## Practical Use Cases

The retention system solves real problems people have with accumulating recordings. Here's how it gets used:

### Personal Use

You record everything during your workday to capture ideas and discussions. Most of these recordings are ephemeral - useful for a week or two, then forgotten. Set a 30-day global retention with audio-only deletion. After a month, the audio files disappear but the searchable transcriptions remain. You can still find what was said in old recordings, but you're not paying to store hours of audio you'll never listen to again.

If something turns out to be important, tag it with a protected tag before the 30 days expire. The tag prevents deletion, preserving both audio and transcript for as long as you need.

### Group Collaboration

Different types of group content need different lifecycles:

| Content Type | Retention Approach | Why |
|--------------|-------------------|-----|
| Daily standups | Group tag with 14-day retention | Routine updates, no long-term value |
| Sprint planning | Group tag with 90-day retention | Reference value for current quarter |
| Architecture decisions | Group tag with protection enabled | Document important choices permanently |
| Customer calls (sales) | Group tag with 1-year retention | Sales cycle duration + follow-up window |
| Interviews (HR) | Group tag with 2-year retention | Typical employment litigation window |
| Legal meetings | Protected tag | Indefinite retention for compliance |

Each group sets up their tags once with appropriate retention. Members just tag recordings normally, and lifecycle management happens automatically. Nobody has to remember which recordings to keep or delete.

### Compliance Requirements

Organizations with data retention policies can enforce them automatically. Healthcare organization needs 7-year retention for patient consultations - set that in the relevant group tag. Law firm needs indefinite retention for client meetings - use protected tags. Financial services deletes routine internal calls after 90 days but keeps compliance-related recordings for 7 years - different tags with different retention.

The system enforces policy without requiring anyone to remember the rules. Tag correctly, and retention happens automatically.

### Storage Cost Management

Audio files are large - a one-hour meeting might be 50-100MB. Transcriptions are text - the same meeting might be 10-20KB. Audio-only deletion mode keeps the valuable searchable text while reclaiming storage.

Run audio-only deletion with a 90-day retention. Recordings older than 90 days lose their audio but remain fully searchable. You can still use Inquire Mode to find information, read transcripts, review summaries, and see notes. You just can't play the original audio. For most use cases, that's fine - once you've extracted the information into text, the audio serves no purpose.

This approach lets you keep years of searchable conversation history without accumulating terabytes of audio files.

## Best Practices

### For Compliance

1. **Set appropriate retention periods**
   ```bash
   # Example: 7-year retention for financial records
   GLOBAL_RETENTION_DAYS=2555  # 7 years × 365 days
   DELETION_MODE=full_recording
   ```

2. **Use tag-based protection** for records requiring indefinite retention
   - Create "Legal Hold" or "Permanent" tags
   - Enable protection on these tags
   - Apply to relevant recordings

3. **Document your retention policy** in your organization's compliance documentation

### For Storage Management

1. **Start with audio-only deletion**
   ```bash
   DELETION_MODE=audio_only
   ```
   - Keeps searchable transcriptions
   - Frees up 95%+ of storage (audio files are large)
   - Maintains business value of conversations

2. **Use shorter retention periods** for routine recordings
   ```bash
   GLOBAL_RETENTION_DAYS=30  # Routine meetings
   ```

3. **Protect important content** with tags
   - "Executive Meetings" tag → protect from deletion
   - "Daily Standup" tag → no protection (routine)

### For Groups

When groups are enabled:
1. **Set conservative global retention** (shorter period as a baseline)
2. **Configure group tags with custom retention** to match each group's needs
3. **Use protected group tags** for group content requiring permanent retention
4. **Document retention policies** so group members understand lifecycle expectations

Example group tag retention configuration:
- Engineering group "Architecture Decisions": Protected (never deleted)
- Sales group "Customer Calls": 365 days
- HR group "Interviews": 90 days
- Operations group "Daily Standups": 14 days

## Deletion Process Flow

```
1. Automated Check (Daily/Manual Trigger)
   ↓
2. Find recordings older than retention period
   ↓
3. For each recording:
   - Check manual exemption flag
   - Check tags for protection
   - Skip if exempt
   ↓
4. Delete based on mode:
   - audio_only: Remove file via storage service, keep DB record, set audio_deleted_at
   - full_recording: Remove file via storage service and DB record
   ↓
5. Return statistics
```

!!! info "S3 Storage Support"
    Audio deletion works transparently with both local and S3 storage backends. The storage service resolves each recording's storage locator (`local://...` or `s3://...`) and deletes the file from the correct backend. If the file is already missing, the deletion is treated as successful and processing continues.

## Migration Guide

### Enabling Auto-Deletion on Existing System

1. **Test with audio-only mode first:**
   ```bash
   ENABLE_AUTO_DELETION=true
   GLOBAL_RETENTION_DAYS=365  # Start with long period
   DELETION_MODE=audio_only   # Test safely
   ```

2. **Protect existing important content:**
   - Create protected tags
   - Apply to critical recordings
   - Verify exemptions via `/admin/auto-deletion/stats`

3. **Run manual test:**
   ```bash
   POST /admin/auto-deletion/run
   ```

4. **Monitor results** and adjust retention period as needed

### Reverting Changes

If you need to disable auto-deletion:
```bash
ENABLE_AUTO_DELETION=false
```

**Note:** Already deleted audio files cannot be recovered. Database records (if using audio-only mode) remain intact.

## API Reference

### Run Auto-Deletion (Admin Only)

```http
POST /admin/auto-deletion/run
```

Manually trigger the auto-deletion process.

### Get Deletion Statistics (Admin Only)

```http
GET /admin/auto-deletion/stats
```

Get statistics about eligible recordings and current configuration.

## Troubleshooting

### Auto-Deletion Not Running

**Check:**
1. `ENABLE_AUTO_DELETION=true` in `.env`
2. `GLOBAL_RETENTION_DAYS > 0`
3. Admin status for manual triggers
4. Server logs for errors

### Too Many Recordings Being Deleted

**Solutions:**
1. Increase `GLOBAL_RETENTION_DAYS`
2. Add protected tags to important categories
3. Check tag assignments on recordings
4. Review exemption status via stats endpoint

### Archived Recordings Not Showing

**Check:**
1. Toggle "Archived Recordings" filter in sidebar
2. Verify `DELETION_MODE=audio_only` (full_recording doesn't archive)
3. Check `audio_deleted_at` field in database

## Security Considerations

1. **Admin-only endpoints**: Auto-deletion triggers require admin authentication
2. **Irreversible deletion**: Deleted audio files cannot be recovered
3. **Audit trail**: Check server logs for deletion events
4. **GDPR compliance**: Full deletion mode helps meet "right to be forgotten" requirements

## Support

For issues or questions about auto-deletion:
1. Check server logs for detailed error messages
2. Verify environment variable configuration
3. Test with `/admin/auto-deletion/stats` endpoint
4. Review this documentation
5. Submit issues on GitHub with logs attached

---

Return to [Admin Guide](index.md) →
