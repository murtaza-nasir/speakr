# Folders

Folders provide a simple way to organize your recordings into logical groups. Unlike tags which can be applied multiple times to categorize content, each recording belongs to exactly one folder (or no folder). Think of folders like directories on your computer - a clean, hierarchical way to keep related recordings together.

## Enabling Folders

Folders are disabled by default. An administrator must enable the feature before users can create and use folders.

**For Administrators:**

1. Go to Admin Panel → System Settings
2. Find the `enable_folders` setting and set it to `true`
3. Save changes

Or set the environment variable:
```bash
ENABLE_FOLDERS=true
```

Once enabled, all users will see folder management options in their Account Settings and folder selectors throughout the interface.

## Creating Folders

Access folder management from **Account Settings → Folders** tab.

Click **Create Folder** to add a new folder. Each folder has:

- **Name** - A short, descriptive name (required)
- **Color** - Visual identifier shown on folder pills and in the sidebar

### Folder Settings

Each folder can have custom settings that apply to recordings placed in it:

| Setting | Description |
|---------|-------------|
| **Custom Prompt** | AI summarization instructions specific to this folder |
| **Default Language** | Transcription language for new recordings |
| **Min/Max Speakers** | Speaker count hints for ASR diarization |
| **Retention Days** | Override global retention for recordings in this folder |
| **Protection** | Exempt folder contents from auto-deletion |

These settings work like tag settings - when you add a recording to a folder, the folder's custom prompt and ASR settings are applied during processing.

## Using Folders

### Sidebar Folder Selector

The sidebar includes a folder dropdown at the top. Use it to:

- **Filter by folder** - Select a folder to show only recordings in that folder
- **View all recordings** - Select "All Folders" to see everything
- **View unfiled** - Select "No Folder" to see recordings not in any folder

### Moving Recordings to Folders

There are several ways to assign recordings to folders:

1. **During upload** - Select a folder before uploading
2. **From recording detail** - Use the folder selector in the recording header
3. **Bulk operations** - Select multiple recordings and use the folder action

### Folder Pills

Recordings display a small colored pill showing their folder name. This appears in:

- The sidebar recording list
- The recording detail header
- Search results

Click the pill to quickly filter to that folder.

### Title Bar Icon

When viewing a recording that's in a folder, a folder icon appears in the title bar next to the recording title. Click it to see folder details or change the folder assignment.

## Group Folders

If your organization uses groups, folders can be group-scoped:

- **Personal folders** - Visible only to you
- **Group folders** - Shared with all group members

Group folders work like group tags with auto-sharing:

| Setting | Description |
|---------|-------------|
| **Auto Share on Apply** | Automatically share recordings with group members when moved to this folder |
| **Share with Group Lead** | Also share with group administrators |

When you move a recording to a group folder with auto-share enabled, all group members receive access automatically.

## Folders vs Tags

Both folders and tags help organize recordings, but they serve different purposes:

| Aspect | Folders | Tags |
|--------|---------|------|
| **Assignment** | One folder per recording | Multiple tags per recording |
| **Purpose** | Primary organization | Categorization and filtering |
| **Hierarchy** | Flat list | Flat list |
| **Settings** | Custom prompt, ASR, retention | Custom prompt, ASR, retention |
| **Visual** | Pill badge, sidebar filter | Colored badges |

**Use folders for:** Project-based organization, client separation, meeting types

**Use tags for:** Cross-cutting concerns, status tracking, topic categorization

You can use both together - put a recording in the "Client A" folder and tag it with "Action Items" and "Follow-up Needed".

## Best Practices

### Naming Conventions

Choose clear, consistent folder names:

- **By project:** "Website Redesign", "Q1 Planning", "Product Launch"
- **By client:** "Acme Corp", "Beta Inc", "Gamma LLC"
- **By type:** "Team Standups", "Client Calls", "Interviews"

### Folder Prompts

Use folder-specific prompts for consistent summaries:

```
For client meetings, create a summary with:
- Discussion topics
- Client requests and concerns
- Commitments made
- Next steps with owners
```

### Retention Strategies

Set folder-level retention to match content needs:

- **Active projects:** No retention limit
- **Completed projects:** 90 days after project close
- **Routine meetings:** 30 days
- **Compliance recordings:** Use protection flag

---

Next: [Tags](settings.md#tag-management-tab) →
