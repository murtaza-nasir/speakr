# Keyboard Shortcuts

Speakr can be used from the keyboard on a computer. A key stands for a button of the page. The same action is carried out under the same conditions. Where the button is absent or disabled, for example on a shared recording without the right to edit, the key has no effect.

The list of keys for the current page is opened with `?`, or with **Keyboard shortcuts** in the account menu. The list is closed with `Esc`.

## Rules

- Keys are single letters and characters, pressed without `Ctrl`, `Alt` or `Cmd`. A character that the keyboard layout types with `AltGr`, such as `#` on some layouts, is accepted. An uppercase letter in the tables is typed with `Shift`. Caps Lock is ignored.
- A sequence such as `g` then `r` is typed as two keys, one after the other. The second key is expected within 1.5 seconds.
- No key is handled while the focus is in a text field, a selection list or an editor, or while text is composed with an input method. `Esc` is the exception where the tables say so.
- On a focused link or button, `Enter`, `Space`, `Tab`, the arrow keys, `Delete` and `Backspace` keep their usual effect. Letters remain shortcuts.
- While a dialog is open, the keys of the page under it are not handled.
- The shortcuts of the browser and of the operating system are not replaced. The one exception is `Ctrl+F` on a long transcript, which is described under [Keys of other parts](#keys-of-other-parts).
- Keyboard shortcuts are not available in the phone layout (a window narrower than 768 px).

## Every page

| Key | Action |
|---|---|
| `?` | Show the keyboard shortcuts |
| `Esc` | Close the dialog, menu or panel on top |
| `g` then `r` | Go to the recordings |
| `g` then `a` | Go to Ask Ambrose, where it is enabled |
| `g` then `s` | Go to the account settings |
| `g` then `d` | Go to the administration, for an administrator |

A dialog is closed with `Esc` as with its Close button. While the focus is in a text field of a dialog, `Esc` is left to that field. A dialog with unsaved input is not closed with `Esc`: a field that was changed since the dialog opened keeps it open. The same holds for the dialogs in which a choice is made with buttons (the date picker, the participants, the merge order and the recovery of a recording) and for the dialogs of the account page. These are closed with their own buttons.

On the Ask Ambrose page, the question field receives the focus with `/`.

## Recording list

| Key | Action |
|---|---|
| `/` | Search recordings |
| `n` | New recording |
| `q` | Show or hide the processing queue, while it has entries |
| `j`, `k` | Move to the next or the previous recording |
| `Enter` or `o` | Open the selected recording |
| `s` | Star or remove the star |
| `i` | Move to the inbox or mark as read |
| `a` | Archive or unarchive |
| `#` | Delete, after the usual confirmation |

The recording selected with `j` and `k` is shown with a frame. The keys `s`, `i`, `a` and `#` apply to that recording. Without a selection of this kind, they apply to the open recording. The selection is returned to the open recording with `Esc`.

The list keys are not handled while the list is hidden or in selection mode. The right to delete the recording is required for `#`.

## Open recording

| Key | Action |
|---|---|
| `1` | Show the summary |
| `2` | Show the notes |
| `3` | Show the speaker stats |
| `4` | Show the events |
| `v` | Switch between simple and bubble view |
| `c` | Copy the transcript |
| `Shift+C` | Copy the summary or the notes, whichever is shown |
| `e` | Edit the transcript |
| `Shift+E` | Edit the summary or the notes, whichever is shown |
| `m` | Open the chat with Ambrose and place the focus in its field |

The transcript is always shown beside these tabs, so it has no key of its own. In the chat field, the panel is closed with `Esc`.

## Player

| Key | Action |
|---|---|
| `p` or `Space` | Play or pause |
| `h`, `l` | Back or forward 5 seconds |
| `Shift+H`, `Shift+L` | Back or forward 15 seconds |
| `<`, `>` | Slower or faster playback, in the steps of the speed menu |

A recording with audio is required for the player keys. `Space` is handled only while no button, link, field or list has the focus. On a focused control it keeps its usual effect, and in a list it scrolls.

## Order of Esc

One thing is closed with each press of `Esc`, in this order: a confirmation or message dialog, another dialog, an open menu, the find bar of the transcript, the chat panel, the processing queue panel, the selection in the list, selection mode.

## Keys of other parts

These keys existed before the scheme and are handled by their own part of the app. They are available whether keyboard shortcuts are on or off.

| Where | Key | Action |
|---|---|---|
| Long transcript | `Ctrl+F` | Find in the transcript. With a second `Ctrl+F` in the find field, the find function of the browser is opened. |
| Segment editor | `?` | Show the keys of the segment editor |
| Segment editor | `Ctrl+S` | Save |
| Segment editor and text editor | `Ctrl+F`, `Ctrl+H` | Find and replace |
| Full-screen video | `Space` or `k` | Play or pause |
| Full-screen video | `←`, `→` | Back or forward 10 seconds |
| Full-screen video | `↑`, `↓` | Volume up or down |
| Full-screen video | `f` or `Esc` | Leave full screen |
| Selection mode of the list | `Ctrl+A` | Select all |
| Selection mode of the list | `Esc` | Leave selection mode |
| Player scrubber on a phone | `←`, `→` | Back or forward 5 seconds |

The segment editor has a larger set of keys for playback, splitting, merging and speakers. Its own list is opened with `?` while the editor is open, and is described under [Advanced ASR Segment Editing](transcripts.md#advanced-asr-segment-editing).

## Turning shortcuts off

Keyboard shortcuts are on for every account. They are turned off, and on again, with **Keyboard shortcuts** under **Account**, **Preferences**. The setting belongs to the account and applies in every browser. While it is off, the keys in the first five tables are not handled, and the entry in the account menu is not shown.
