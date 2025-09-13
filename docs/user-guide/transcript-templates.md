# Transcript Templates Guide

## Overview

Transcript Templates in Speakr allow you to customize how your transcriptions are formatted when downloading them. Instead of a fixed format, you can create multiple templates for different use cases - whether you need timestamps for subtitles, speaker-focused formats for interviews, or screenplay-style formatting for media production.

## Accessing Transcript Templates

1. Click on your username in the top-right corner
2. Navigate to **Account Settings**
3. Select the **Transcript Templates** tab

## Understanding Template Variables

Templates use placeholders (variables) that get replaced with actual transcript data:

### Available Variables

- `{{index}}` - Sequential number of the transcript segment (1, 2, 3...)
- `{{speaker}}` - Name of the speaker (e.g., "Speaker 1", "John", etc.)
- `{{text}}` - The actual spoken text/content
- `{{start_time}}` - When the segment starts (format: HH:MM:SS)
- `{{end_time}}` - When the segment ends (format: HH:MM:SS)

### Filters

Variables can be modified using filters by adding a pipe (|) symbol:

- `{{speaker|upper}}` - Converts speaker name to UPPERCASE
- `{{text|upper}}` - Converts text to UPPERCASE
- `{{start_time|srt}}` - Formats time for SRT subtitles (HH:MM:SS,mmm)
- `{{end_time|srt}}` - Formats time for SRT subtitles (HH:MM:SS,mmm)

## Creating Your First Template

### Step 1: Click "Create Template"
In the Transcript Templates section, click the **Create Template** button.

### Step 2: Fill in Template Details

**Template Name**: Give your template a descriptive name
- Examples: "Interview Format", "Subtitles SRT", "Meeting Minutes"

**Description** (Optional): Add a brief description of when to use this template
- Example: "Use for client interview transcriptions"

**Template**: Enter your format pattern using variables
- Example: `[{{start_time}}] {{speaker}}: {{text}}`

### Step 3: Set Default (Optional)
Check "Set as default template" if you want this template to be pre-selected when downloading transcripts.

### Step 4: Save
Click **Save** to create your template.

## Common Template Examples

### 1. Simple Conversation
```
{{speaker}}: {{text}}
```
**Output:**
```
John: Hello, how are you today?
Sarah: I'm doing great, thanks for asking!
```

### 2. Timestamped Format
```
[{{start_time}} - {{end_time}}] {{speaker}}: {{text}}
```
**Output:**
```
[00:00:01 - 00:00:03] John: Hello, how are you today?
[00:00:04 - 00:00:07] Sarah: I'm doing great, thanks for asking!
```

### 3. Interview/Q&A Format
```
{{speaker|upper}}:
{{text}}

```
**Output:**
```
INTERVIEWER:
What brought you to this field?

GUEST:
I've always been passionate about technology...
```

### 4. SRT Subtitle Format
```
{{index}}
{{start_time|srt}} --> {{end_time|srt}}
{{text}}

```
**Output:**
```
1
00:00:01,000 --> 00:00:03,000
Hello, how are you today?

2
00:00:04,000 --> 00:00:07,000
I'm doing great, thanks for asking!
```

### 5. Meeting Minutes Style
```
• [{{start_time}}] {{speaker}}: {{text}}
```
**Output:**
```
• [00:00:01] John: Let's begin today's meeting with updates.
• [00:00:05] Sarah: I'll start with the marketing report.
```

### 6. Screenplay Format
```
                    {{speaker|upper}}
        {{text}}

```
**Output:**
```
                    JOHN
        Hello, how are you today?

                    SARAH
        I'm doing great, thanks for asking!
```

### 7. Court Transcript Style
```
{{index}}    {{speaker|upper}}: {{text}}
```
**Output:**
```
1    ATTORNEY: Please state your name for the record.
2    WITNESS: My name is John Smith.
```

## Using Templates When Downloading

### Method 1: From the Transcript View
1. Open a recording with a transcription
2. Click the **Download** button next to the transcript
3. Select your desired template from the popup
4. The transcript will download in your chosen format

### Method 2: Set a Default Template
1. Edit a template and check "Set as default template"
2. This template will be pre-selected when downloading
3. You can still choose a different template at download time

### Method 3: Download Raw Transcript
- Select "No Template (Raw Export)" to download the transcript without any formatting
- Useful when you need to process the text in another application

## Managing Templates

### Editing Templates
1. Click on any template in the list to open it
2. Modify the name, description, or format
3. Click **Save** to update

### Deleting Templates
1. Open the template you want to delete
2. Click the **Delete** button
3. Confirm the deletion

### Creating Default Templates
If you have no templates, click **Create Default Templates** to generate starter templates:
- Simple with Timestamps
- Screenplay Format
- Interview Q&A

## Advanced Tips

### 1. Multi-line Templates
You can create templates with multiple lines by adding line breaks:
```
=================
Time: {{start_time}}
Speaker: {{speaker}}
Message: {{text}}
=================
```

### 2. Combining Filters
Use multiple filters in the same template:
```
[{{start_time|srt}}] {{speaker|upper}}: {{text}}
```

### 3. Creating Separators
Add visual separators between segments:
```
{{speaker}}: {{text}}
---
```

### 4. Indentation for Readability
Use spaces or tabs for indentation:
```
    {{start_time}} | {{speaker}}
        {{text}}
```

## Use Cases

### For Journalists
Create an "Interview Format" template with timestamps and speaker labels for easy reference when writing articles.

### For Researchers
Use a numbered format with timestamps to cite specific moments in recorded interviews or focus groups.

### For Content Creators
Export in SRT format for adding subtitles to videos, or use screenplay format for video scripts.

### For Business Meetings
Create a "Meeting Minutes" template that clearly shows who said what and when, perfect for action items and follow-ups.

### For Legal Professionals
Use court transcript style formatting with line numbers and uppercase speaker names for depositions and testimonies.

### For Podcasters
Format transcripts for show notes with timestamps that listeners can click to jump to specific topics.

## Troubleshooting

### Template Not Appearing
- Ensure you've saved the template
- Refresh the page if needed
- Check that you're logged into the correct account

### Variables Not Replacing
- Verify variable names are spelled correctly
- Ensure double curly braces `{{}}` are used
- Check that the transcript has the required data (speakers, timestamps)

### Download Issues
- Ensure the recording has a completed transcription
- Try downloading without a template first to verify the transcript exists
- Check your browser's download settings

## Best Practices

1. **Name Templates Clearly**: Use descriptive names that indicate the purpose
2. **Test Your Templates**: Download a sample transcript to verify formatting
3. **Keep Multiple Templates**: Create different templates for different use cases
4. **Document Complex Templates**: Use the description field to explain when to use each template
5. **Start Simple**: Begin with basic templates and add complexity as needed

## Need Help?

If you encounter issues or have questions about transcript templates:
1. Check this guide for examples and troubleshooting
2. Try the default templates as a starting point
3. Report issues at [GitHub Issues](https://github.com/anthropics/claude-code/issues)

---

*Last updated: December 2024*