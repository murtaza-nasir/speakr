---
layout: default
title: Ask Ambrose
parent: User Guide
nav_order: 5
---

# Ask Ambrose (formerly Inquire)

**Ask Ambrose** is the library page of Speakr. On it you ask questions about all your recordings at once, or about the recordings that match a set of filters. Each answer has numbered citations. Click one to open its recording at the moment something was said. To ask about one recording only, use the recording chat (see [Ambrose](ambrose.md#the-recording-chat)).

Ambrose is the default name of the assistant. You can give it another name under Account → Preferences → **Assistant** (see [Settings](settings.md#assistant)). The page, the header button and the recording chat then use that name. This guide uses the default.

![The Ask Ambrose page, with the filter column on the left and an answer with sources](../assets/images/screenshots/inquire-semantic-search.png)

## Opening the page

Click **Ask Ambrose** in the header, next to **+ New Recording**. The button carries Ambrose's monogram, the first letter of the name in a circle. The page lives at `/inquire`, and `/ask` opens the same page.

The filters are on the left and the conversation on the right. Above the first question, a scope chip shows what Ambrose searches: **All recordings**, or a short summary of the active filters, such as "2 tags · Date Range". Type the question as you would ask a colleague and press Enter to send it. For a new line, press Ctrl+Enter. The same hint is shown on the send button when you hover over it.

Ambrose searches by meaning as well as by words. A question about "budget concerns" also finds discussions of cost overruns, funding or resource limits, even when those exact words were never spoken.

!!! tip "+ New Recording from the Ask page"
    The **+ New Recording** button in the header of the Ask page opens the main page with the upload dialog already open (`/?upload=1`). The parameter is removed from the address afterwards, so a refresh does not open the dialog again.

## Using filters to focus the search

The left column holds filters that limit the search to some recordings before you ask. Filters help most in a large library.

- **Tags**: recordings with the selected tags, for example one project, one client or one kind of meeting. The count beside each tag is the number of recordings that carry it.
- **Folders**, when folders are enabled: recordings in the selected folders. When tags and folders are both selected, only recordings that are in one of the selected folders and carry one of the selected tags are searched.
- **Speakers**: what specific people said across recordings, for example to track the commitments of one person.
- **Date range**: recordings from a period, such as one quarter or one project phase.

The scope chip is updated as you change the filters. Ambrose can narrow a search within the filters and never goes beyond them.

## Asking effective questions

Answers are best when questions are clear and specific. Give context and say what you are looking for. Instead of a single word such as "deadline", ask a full question such as "What are the upcoming deadlines for the mobile app project?"

Useful kinds of questions:

- summaries of one topic across several meetings
- lists of action items or decisions
- specific statements or commitments, such as what was promised to a client
- how a discussion changed over time
- problems that were raised and the solutions that were proposed

Start with a broad question, then ask follow-up questions to go deeper. For a client meeting, for example, select the client's tag and a date range, then ask about open issues. If an answer misses the point, rephrase the question with the words people used in the recordings.

For questions about one recording, open it and use the recording chat, where Ambrose reads the whole transcript.

## Understanding the answer

Ambrose gives one answer that draws on all the relevant recordings, with related points grouped together. Each statement carries a citation such as "2 @ 14:05". Click it to open that recording and start playback at the cited moment. A **Sources** list at the end of each answer gives every number with its recording. See [Citations](ambrose.md#citations) for details.

## Agent mode (beta)

When your administrator has turned on the agent, Ambrose works in steps. It searches your transcripts, lists recordings, and reads transcripts, summaries or notes until it can answer. For the full list, see [What Ambrose can search](ambrose.md#what-ambrose-can-search).

- Each step is shown live: what was searched, what was read and how many matches were found.
- A **Stop** button interrupts the answer.
- Each answer has a collapsed activity line, such as "4 steps · 3 listings · 1 search". Open it to see how Ambrose reached the answer.

Some examples:

- "What did we decide about the pricing change, and did anyone disagree?" Ambrose searches for the pricing discussions, reads the relevant parts of several meetings and cites each position where it was said.
- "Summarize everything client X asked for this quarter." Ambrose lists the recordings with that client, reads their summaries and gives one citation per request.
- "When did I first mention the migration plan?" Ambrose searches, compares dates and links to the sentence.

Answers in agent mode take longer, typically ten to thirty seconds for questions with several steps. Without the agent, Ambrose answers from one search of your library.

Ambrose reads summaries and notes only as far as your settings allow. Under Account → Prompt options → **What Ambrose can read** (formerly Inquire Privacy), summaries are on by default and notes are off by default (see [Settings](settings.md#what-ambrose-can-read)). Only your own notes are used, never notes on recordings shared with you.

### Long conversations

When a conversation grows past the context budget, the older turns are folded into a summary that Ambrose keeps. A divider is shown at that place in the conversation. Click it to see what Ambrose still remembers. Once the context is more than half full, a small meter next to the input gives how full it is.

## Downloading the conversation

Once the conversation has a question or an answer, a header appears above it. On the left are Ambrose's monogram, the word **Conversation** and the scope chip. On the right are two buttons: a download button and a **Clear conversation** button.

With the download button you save the questions and answers on screen as a Word file named `inquire.docx`, titled "Ask Ambrose" (with the name you chose).

- Headings, lists, bold text and links in the answers are kept.
- Links become Word hyperlinks with full addresses, so a link to a recording opens that recording in Speakr. The address starts with `APP_BASE_URL` when the administrator has set it, and with the address you used to open Speakr otherwise.
- Each answer is followed by its Sources list, numbered as on screen, and the citation markers in the answer carry the same numbers, such as "2 @ 14:05". Each source title links to its recording.
- When tags, folders, speakers or dates are selected, their names appear under the export date.
- An answer that is still streaming, or one you stopped, is saved as far as it got.

Each finished answer also has a copy button and a download button at its bottom right, below its Sources list. With the copy button you copy the answer's text as Markdown. With the download button you save that answer and the question before it as a Word file named `inquire-response.docx`, with the same layout as the conversation download. The two buttons appear once the answer has finished streaming.

**Clear conversation** removes the questions and answers and starts a new conversation. The filters stay as they are. An answer that is still streaming is stopped first. **Clear all** in the filter column removes the filters as well as the conversation.

The conversation on the Ask page is not stored. Download it before you leave or refresh the page.

## Searching from other tools

Search by meaning is also available through the API: `GET /api/v1/search?mode=semantic` (see the [API Reference](api-reference.md#search)). The same request without `mode=semantic` is a keyword search over every recording, available without library search.

## Limitations

- Ambrose searches the transcribed text, not the audio, so errors in a transcript affect the answers.
- Ambrose reports what was said. It does not infer what was meant and left unsaid.
- A new recording becomes searchable once its processing has finished.
- Results in languages other than English depend on the embedding model your administrator has chosen.

Clear audio, named speakers and consistent tags give better answers.

For administrators: see [Ambrose settings](../admin-guide/ambrose.md) for the settings, the models and the limits.

---

Next: [Sharing Recordings](sharing.md) →
