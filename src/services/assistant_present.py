"""How Ambrose's work is shown: step labels, step results and the collapsed
steps line, built on the server and translated (Ambrose phase 2a).

Labels name what the assistant does in the user's terms ("Searching for
“budget”", "Reading the transcript of Weekly Sync"): recording titles, never
ids. The library engine's retrieval stages (RAG `status` events) map onto the
same steps, so both engines look alike in the interface.

Every translate() key is written out in full so the locale tests can see it.
"""

from src.utils.localization import translate

TOOL_ICONS = {
    'search_transcripts': 'fa-magnifying-glass',
    'list_recordings': 'fa-list',
    'get_transcript': 'fa-file-lines',
    'get_summary': 'fa-file-alt',
    'get_notes': 'fa-note-sticky',
    'get_recording_metadata': 'fa-circle-info',
}

# RAG stage (the `status` value inquire_stream sends) -> step icon.
RAG_ICONS = {
    'processing': 'fa-comment-dots',
    'enriching': 'fa-wand-magic-sparkles',
    'searching': 'fa-magnifying-glass',
    'filtering': 'fa-user',
    'requerying': 'fa-arrows-left-right',
    'contextualizing': 'fa-layer-group',
    'fetching': 'fa-file-lines',
    'responding': 'fa-pen',
    'compacting': 'fa-compress',
}


def tool_icon(tool):
    return TOOL_ICONS.get(tool, 'fa-gear')


def step_label(tool, args, language=None, title=None):
    """The running label of one tool step. `title` is the title of the
    recording the tool reads, when the caller may show it."""
    args = args or {}
    if tool == 'search_transcripts':
        return translate('server.assistant.step.searching', language, query=str(args.get('query', ''))[:80])
    if tool == 'list_recordings':
        return translate('server.assistant.step.listing', language)
    if tool == 'get_transcript':
        if not title:
            return translate('server.assistant.step.readingATranscript', language)
        offset = args.get('offset') or 0
        if offset:
            return translate('server.assistant.step.readingTranscriptFrom', language, title=title, offset=offset)
        return translate('server.assistant.step.readingTranscript', language, title=title)
    if tool == 'get_summary':
        if not title:
            return translate('server.assistant.step.readingASummary', language)
        return translate('server.assistant.step.readingSummary', language, title=title)
    if tool == 'get_notes':
        if not title:
            return translate('server.assistant.step.readingSomeNotes', language)
        return translate('server.assistant.step.readingNotes', language, title=title)
    if tool == 'get_recording_metadata':
        if not title:
            return translate('server.assistant.step.checkingARecording', language)
        return translate('server.assistant.step.checking', language, title=title)
    return translate('server.assistant.step.running', language, tool=tool)


# Counted phrases. Each key is written out so the locale tests can find it;
# "one" has its own string, every other count uses the {{count}} form.
_COUNTED = {
    'match': lambda n, lang: (translate('server.assistant.result.matchOne', lang) if n == 1
                              else translate('server.assistant.result.matches', lang, count=n)),
    'recording': lambda n, lang: (translate('server.assistant.result.recordingOne', lang) if n == 1
                                  else translate('server.assistant.result.recordings', lang, count=n)),
    'step': lambda n, lang: (translate('server.assistant.count.stepOne', lang) if n == 1
                             else translate('server.assistant.count.steps', lang, count=n)),
    'search_transcripts': lambda n, lang: (translate('server.assistant.count.searchOne', lang) if n == 1
                                           else translate('server.assistant.count.searches', lang, count=n)),
    'list_recordings': lambda n, lang: (translate('server.assistant.count.listingOne', lang) if n == 1
                                        else translate('server.assistant.count.listings', lang, count=n)),
    'get_transcript': lambda n, lang: (translate('server.assistant.count.transcriptReadOne', lang) if n == 1
                                       else translate('server.assistant.count.transcriptReads', lang, count=n)),
    'get_summary': lambda n, lang: (translate('server.assistant.count.summaryReadOne', lang) if n == 1
                                    else translate('server.assistant.count.summaryReads', lang, count=n)),
    'get_notes': lambda n, lang: (translate('server.assistant.count.notesReadOne', lang) if n == 1
                                  else translate('server.assistant.count.notesReads', lang, count=n)),
    'get_recording_metadata': lambda n, lang: (translate('server.assistant.count.detailsCheckOne', lang) if n == 1
                                               else translate('server.assistant.count.detailsChecks', lang, count=n)),
}


def step_result(tool, result, language=None):
    """(status, detail) once a tool has run."""
    if isinstance(result, dict) and result.get('error'):
        return 'error', translate('server.assistant.result.failed', language)
    result = result if isinstance(result, dict) else {}
    if tool == 'search_transcripts':
        n = result.get('result_count', 0)
        if not n:
            return 'empty', translate('server.assistant.result.noMatches', language)
        recordings = len({r.get('recording_id') for r in result.get('results', [])})
        detail = translate('server.assistant.result.matchSummary', language,
                           matches=_COUNTED['match'](n, language),
                           recordings=_COUNTED['recording'](recordings, language))
        search_type = result.get('search_type')
        if search_type == 'keyword':
            detail += ' · ' + translate('server.assistant.result.keyword', language)
        elif search_type and search_type != 'semantic':
            detail += ' · ' + translate('server.assistant.result.withKeyword', language)
        return 'done', detail
    if tool == 'list_recordings':
        return 'done', translate('server.assistant.result.listed', language,
                                 returned=result.get('returned', 0), total=result.get('total_in_scope', 0))
    if tool == 'get_transcript':
        if 'total_segments' in result:
            return 'done', translate('server.assistant.result.segments', language,
                                     returned=result.get('returned_segments', 0),
                                     total=result.get('total_segments', '?'))
        return 'done', ''
    return 'done', ''


def summary_line(tool_counts, language=None):
    """'3 searches · 1 transcript read': what the steps were, by kind."""
    parts = []
    for tool, n in tool_counts.items():
        if tool in _COUNTED:
            parts.append(_COUNTED[tool](n, language))
        else:
            parts.append(translate('server.assistant.count.other', language, tool=tool, count=n))
    return ' · '.join(parts)


def summary_label(steps, tool_counts, language=None):
    """'4 steps · 3 searches · 1 transcript read': the collapsed steps line."""
    head = _COUNTED['step'](steps, language)
    line = summary_line(tool_counts, language)
    return f'{head} · {line}' if line else head


def rag_step_label(stage, language=None):
    """The step label for one retrieval stage of the library engine, or None
    for a stage that is not shown as a step."""
    if stage == 'processing':
        return translate('server.assistant.step.understanding', language)
    if stage == 'enriching':
        return translate('server.assistant.step.refining', language)
    if stage == 'searching':
        return translate('server.assistant.step.searchingLibrary', language)
    if stage == 'filtering':
        return translate('server.assistant.step.narrowing', language)
    if stage == 'requerying':
        return translate('server.assistant.step.widening', language)
    if stage == 'contextualizing':
        return translate('server.assistant.step.gathering', language)
    if stage == 'fetching':
        return translate('server.assistant.step.readingFull', language)
    if stage == 'responding':
        return translate('server.assistant.step.writing', language)
    if stage == 'compacting':
        return translate('server.assistant.step.compacting', language)
    return None
