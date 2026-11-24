"""
Semantic search and chat functionality.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
import re
from src.utils.timezones import to_local, user_timezone
from src.utils.route_errors import internal_error
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response, current_app
from flask_login import login_required, current_user

from src.database import db
from src.models import *
from src.utils import *
from src.utils.localization import translate, api_error
from src.utils.assistant_limits import checked_history, checked_question, history_trimmed_event
from src.utils.error_text import log_failure, user_error_text
from src.utils.speaker_labels import real_speaker_names
from src.services.assistant_events import FilterError, checked_filters, filters_from_request
from src.services.embeddings import get_accessible_recording_ids, semantic_search_chunks, EMBEDDINGS_AVAILABLE
from src.services import llm_config
from src.services.llm import call_chat_completion, get_chat_config, process_streaming_with_thinking, TokenBudgetExceeded
from src.utils.request_body import json_object_body

# Create blueprint
inquire_bp = Blueprint('inquire', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'

# Most chunks POST /api/inquire/search returns (#628)
INQUIRE_SEARCH_MAX_TOP_K = 50
# InquireSession.session_name is a String(200) (#629)
INQUIRE_SESSION_NAME_MAX_CHARS = 200

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None

def init_inquire_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


# --- Routes ---

@inquire_bp.route('/inquire')
@inquire_bp.route('/ask')
@login_required
def inquire():
    """The library page, "Ask Ambrose" (the assistant's name,
    src/utils/assistant_name.py). /ask is an alias of /inquire."""
    # Check if inquire mode is enabled
    if not ENABLE_INQUIRE_MODE:
        flash(translate('server.flash.inquireDisabled'), 'warning')
        return redirect(url_for('recordings.index'))

    # Check if user is a group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    # Render the inquire page with user context for theming
    # The interface language, as on the other pages (it was missing here, so
    # Inquire always appeared in the browser language).
    user_language = current_user.ui_language or 'en'
    from src.utils.assistant_name import assistant_name_of
    page_title = translate('inquire.title', user_language, name=assistant_name_of(current_user))
    return render_template('inquire.html',
                         page_title=page_title,
                         use_asr_endpoint=USE_ASR_ENDPOINT,
                         current_user=current_user,
                         is_team_admin=is_team_admin,
                         user_language=user_language)



@inquire_bp.route('/api/inquire/sessions', methods=['GET'])
@login_required
def get_inquire_sessions():
    """Get all inquire sessions for the current user."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify(api_error('inquire_disabled')), 403
    try:
        sessions = InquireSession.query.filter_by(user_id=current_user.id).order_by(InquireSession.last_used.desc()).all()
        return jsonify([session.to_dict() for session in sessions])
    except Exception as e:
        current_app.logger.error(f"Error getting inquire sessions: {e}")
        return internal_error(e)



@inquire_bp.route('/api/inquire/sessions', methods=['POST'])
@login_required
def create_inquire_session():
    """Create a new inquire session with filters."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify(api_error('inquire_disabled')), 403
    try:
        data, body_error = json_object_body()
        if body_error:
            return body_error
        if not data:
            return jsonify(api_error('no_data_provided')), 400

        try:
            checked = checked_filters(data)
        except FilterError as e:
            return jsonify(api_error(e.code)), 400
        # The column holds 200 characters (#629)
        name = data.get('session_name')
        if name is not None and (not isinstance(name, str) or len(name) > INQUIRE_SESSION_NAME_MAX_CHARS):
            return jsonify(api_error('invalid_session_name', max=INQUIRE_SESSION_NAME_MAX_CHARS)), 400
        # A session is not saved around a recording the caller cannot search:
        # the filter would match nothing. Refused, never silently narrowed (#629).
        if checked['filter_recording_ids']:
            allowed = set(get_accessible_recording_ids(current_user.id))
            if any(rid not in allowed for rid in checked['filter_recording_ids']):
                return jsonify(api_error('invalid_recording_ids')), 400

        session = InquireSession(
            user_id=current_user.id,
            session_name=name,
            filter_tags=json.dumps(checked['filter_tags']),
            filter_speakers=json.dumps(real_speaker_names(checked['filter_speakers'])),
            filter_date_from=checked['filter_date_from'],
            filter_date_to=checked['filter_date_to'],
            filter_recording_ids=json.dumps(checked['filter_recording_ids'])
        )

        db.session.add(session)
        db.session.commit()

        return jsonify(session.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        log_failure(current_app.logger, 'error', "Error creating inquire session", e)
        return jsonify(api_error('unexpected_error')), 500



@inquire_bp.route('/api/inquire/search', methods=['POST'])
@login_required
def inquire_search():
    """Perform semantic search within filtered transcriptions.

    top_k: the number of chunks to return, 1 to 50 (default 5); any other
    value is refused with 400.
    """
    if not ENABLE_INQUIRE_MODE:
        return jsonify(api_error('inquire_disabled')), 403
    try:
        data, body_error = json_object_body()
        if body_error:
            return body_error
        if not data:
            return jsonify(api_error('no_data_provided')), 400
        
        # A non-blank string within the question limit (#673)
        query, query_error = checked_question(data.get('query'), api_error('no_query_provided'))
        if query_error:
            return jsonify(query_error), 400
        
        # Build filters from request: the same filters, and the same checks,
        # as the chat route (diarization labels as speakers are ignored)
        try:
            filters = filters_from_request(data)
        except FilterError as e:
            return jsonify(api_error(e.code)), 400

        # Perform semantic search: top_k is a whole number from 1 to 50 (#628)
        top_k = data.get('top_k', 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            return jsonify(api_error('invalid_integer_value')), 400
        if not 1 <= top_k <= INQUIRE_SEARCH_MAX_TOP_K:
            return jsonify(api_error('invalid_top_k', max=INQUIRE_SEARCH_MAX_TOP_K)), 400
        chunk_results = semantic_search_chunks(current_user.id, query, filters, top_k)
        
        # Format results
        results = []
        for chunk, similarity in chunk_results:
            result = chunk.to_dict()
            result['similarity'] = similarity
            result['recording_title'] = chunk.recording.title
            result['recording_meeting_date'] = chunk.recording.meeting_date.isoformat() if chunk.recording.meeting_date else None
            results.append(result)
        
        return jsonify({'results': results})

    except Exception as e:
        log_failure(current_app.logger, 'error', "Error in inquire search", e)
        return jsonify(api_error('unexpected_error')), 500



def inquire_stream(user, data, mode='auto', tool_names=None):
    """The Inquire answer as an SSE generator, for the web route and API v1.

    data: message, message_history, conversation_memory, filter_tags,
    filter_folders, filter_speakers, filter_recording_ids, filter_date_from, filter_date_to.
    mode: 'auto' (the agent when ENABLE_INQUIRE_AGENT is on), 'rag' or
    'agent' (the caller checks that the agent is on).
    tool_names: the agent's tools for this surface
    (assistant_events.tools_for_surface); None offers them all.
    """
    user_message = data.get('message')
    # The routes refuse a history that is not a list of messages; this bound
    # also holds for a caller that did not check (#633).
    message_history = checked_history(data.get('message_history'))[0] or []
    # Build filters from request (the Ask page's filter column)
    from src.services.assistant_events import filters_from_request
    filters = filters_from_request(data)
    
    # The query text is user content: DEBUG only (discussion #603).
    current_app.logger.info(
        f"Inquire chat - User: {user.id}, Query: {len(user_message or '')} characters, "
        f"Filters: {dict(filters, speaker_names=len(filters['speaker_names'])) if isinstance(filters, dict) and filters.get('speaker_names') else filters}")
    current_app.logger.debug(f"Inquire chat query of user {user.id}: '{user_message}'")
    
    # Capture user context and app before generator to avoid context issues
    user_id = user.id
    user_zone = user_timezone(user)   # read here: the stream runs without the request
    user_name = user.name if user.name else "the user"
    user_title = user.job_title if user.job_title else "professional"
    user_company = user.company if user.company else "their organization"
    user_output_language = user.output_language if user.output_language else None
    from src.utils.assistant_name import assistant_name_of, assistant_identity
    from src.utils.localization import request_language
    assistant_name = assistant_name_of(user)
    ui_language = request_language()   # step labels; the stream runs without the request
    app = current_app._get_current_object()  # Capture app for use in generator
    
    # Enhanced query processing with enrichment and debugging
    def create_status_response(status, message):
        """Helper to create SSE status updates"""
        return f"data: {json.dumps({'status': status, 'message': message})}\n\n"
    
    def generate_enhanced_chat():
        # Explicitly reference outer scope variables
        nonlocal user_id, user_name, user_title, user_company, user_output_language, data, filters

        # Push app context for entire generator execution
        # This is needed because call_chat_completion uses current_app.logger internally
        ctx = app.app_context()
        ctx.push()

        try:
            # Send initial status
            yield create_status_response('processing', 'Analyzing your query...')
            
            # Step 1: Router - Determine if RAG lookup is needed
            router_prompt = f"""Analyze this user query to determine if it requires searching through transcription content or if it's a simple formatting/clarification request.

User query: "{user_message}"

Respond with ONLY "RAG" if the query requires searching transcriptions (asking about content, conversations, specific information from recordings).
Respond with ONLY "DIRECT" if it's a formatting request, clarification about previous responses, or doesn't require searching transcriptions.

Examples:
- "What did Beth say about the budget?" → RAG
- "Can you format this in separate headings?" → DIRECT  
- "Who mentioned the timeline?" → RAG
- "Make this more structured" → DIRECT"""

            try:
                router_response = call_chat_completion(
                    messages=[
                        {"role": "system", "content": "You are a query router. Respond with only 'RAG' or 'DIRECT'."},
                        {"role": "user", "content": router_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=10,
                    user_id=user_id,
                    operation_type='query_routing'
                )

                raw_decision = router_response.choices[0].message.content
                if not raw_decision or not raw_decision.strip():
                    app.logger.warning("Router returned empty response, defaulting to RAG")
                    raise ValueError("Empty router response")

                route_decision = raw_decision.strip().upper()
                # The model's raw answer: only the two known decisions at INFO.
                app.logger.info(
                    f"Router decision: {route_decision if route_decision in ('DIRECT', 'RAG') else 'other'}")
                app.logger.debug(f"Router raw decision: {route_decision}")

                if route_decision == "DIRECT":
                    # Direct response without RAG lookup
                    yield create_status_response('responding', 'Generating direct response...')

                    direct_prompt = f"""{assistant_identity(assistant_name, user_name, user_title, user_company)} Respond to their request directly using proper markdown formatting.

User request: "{user_message}"

Previous conversation context (if relevant):
{json.dumps([{'role': m['role'], 'content': m['content']} for m in message_history[-2:]] if message_history else [])}

Use proper markdown formatting including headings (##), bold (**text**), bullet points (-), etc."""

                    stream = call_chat_completion(
                        messages=[
                            {"role": "system", "content": direct_prompt},
                            {"role": "user", "content": user_message}
                        ],
                        temperature=0.7,
                        max_tokens=int(os.environ.get("CHAT_MAX_TOKENS", "2000")),
                        stream=True,
                        user_id=user_id,
                        operation_type='chat'
                    )

                    # Use helper function to process streaming with thinking tag support
                    for response in process_streaming_with_thinking(stream, user_id=user_id, operation_type='chat', model_name=get_chat_config()['model_name']):
                        yield response
                    return
                    
            except Exception as e:
                log_failure(app.logger, 'warning', "Router failed, defaulting to RAG", e)
            
            # Step 2: Query enrichment - generate better search terms based on user intent
            yield create_status_response('enriching', 'Enriching search query...')
            
            # Use captured user context for personalized search terms
            
            if EMBEDDINGS_AVAILABLE:
                enrichment_prompt = f"""You are a query enhancement assistant. Given a user's question about transcribed meetings/recordings, generate 3-5 alternative search terms or phrases that would help find relevant content in a semantic search system.

User context:
- Name: {user_name}
- Title: {user_title}
- Company: {user_company}

User question: "{user_message}"
Available context: Transcribed meetings and recordings with speakers: {', '.join(data.get('filter_speakers', []))}.

Generate search terms that would find relevant content. Focus on:
1. Key concepts and topics using the user's actual name instead of generic terms like "me"
2. Specific terminology that might be used in their professional context
3. Alternative phrasings of the question with proper names
4. Related terms that might appear in transcripts from their meetings

Examples:
- Instead of "what Beth told me" use "what Beth told {user_name}"
- Instead of "my last conversation" use "{user_name}'s conversation"
- Use their job title and company context when relevant

Respond with only a JSON array of strings: ["term1", "term2", "term3", ...]"""
            else:
                enrichment_prompt = f"""You are a keyword extraction assistant. Given a user's question about transcribed meetings/recordings, extract 3-5 essential keyword phrases for a basic text search (SQL LIKE matching, not semantic search).

User context:
- Name: {user_name}

User question: "{user_message}"

Rules:
- Return ONLY the key terms that would actually appear in a transcript — no filler words
- Each term should be 1-3 words maximum
- Replace pronouns like "me", "my", "I" with the user's name "{user_name}"
- Focus on proper nouns, topic-specific terms, and distinctive phrases
- Do NOT include common words like "meeting", "discussion", "plan", "talk" unless they are the actual topic

Examples:
- "what is up with Railroad Retirement" → ["Railroad Retirement", "railroad", "retirement"]
- "when did Beth mention the budget deadline" → ["Beth", "budget deadline", "budget"]
- "what did we discuss about AI foresight" → ["AI foresight", "{user_name}", "foresight"]

Respond with only a JSON array of strings: ["term1", "term2", ...]"""
            
            try:
                enrichment_response = call_chat_completion(
                    messages=[
                        {"role": "system", "content": "You are a query enhancement assistant. Respond only with valid JSON arrays of search terms."},
                        {"role": "user", "content": enrichment_prompt}
                    ],
                    temperature=0.3,
                    max_tokens=200,
                    user_id=user_id,
                    operation_type='query_enrichment'
                )

                raw_content = enrichment_response.choices[0].message.content
                if not raw_content or not raw_content.strip():
                    app.logger.warning(f"Query enrichment returned empty response")
                    raise ValueError("Empty response from LLM")

                # Try to extract JSON array if wrapped in other text
                content = raw_content.strip()
                if content.startswith('['):
                    enriched_terms = json.loads(content)
                else:
                    # Try to find JSON array in the response
                    match = re.search(r'\[.*?\]', content, re.DOTALL)
                    if match:
                        enriched_terms = json.loads(match.group())
                    else:
                        app.logger.warning(f"Query enrichment response not JSON ({len(content)} characters)")
                        app.logger.debug(f"Query enrichment response: {content[:200]}")
                        raise ValueError("No JSON array found in response")

                app.logger.info(f"Enriched search terms: {len(enriched_terms)}")
                app.logger.debug(f"Enriched search terms: {enriched_terms}")
                
                # Combine original query with enriched terms for search
                search_queries = [user_message] + enriched_terms[:3]  # Use original + top 3 enriched terms
                
            except Exception as e:
                log_failure(app.logger, 'warning', "Query enrichment failed, using original query", e)
                search_queries = [user_message]
            
            # Step 2: Semantic search with multiple queries
            yield create_status_response('searching', 'Searching transcriptions...')
            
            all_chunks = []
            seen_chunk_ids = set()
            
            for query in search_queries:
                with app.app_context():
                    chunk_results = semantic_search_chunks(user_id, query, filters, 8)
                    app.logger.info(f"Search query ({len(query)} characters) returned {len(chunk_results)} chunks")
                    app.logger.debug(f"Search query '{query}' returned {len(chunk_results)} chunks")
                
                for chunk, similarity in chunk_results:
                    if chunk and chunk.id not in seen_chunk_ids:
                        all_chunks.append((chunk, similarity))
                        seen_chunk_ids.add(chunk.id)
            
            # Sort by similarity and take top results
            all_chunks.sort(key=lambda x: x[1], reverse=True)
            chunk_results = all_chunks[:data.get('context_chunks', 8)]

            app.logger.info(f"Final chunk results: {len(chunk_results)} chunks with similarities: {[f'{s:.3f}' for _, s in chunk_results]}")
            
            # Step 2.5: Auto-detect mentioned speakers and apply filters if needed
            with app.app_context():
                # Get available speakers
                recordings_with_participants = Recording.query.filter_by(user_id=user_id).filter(
                    Recording.participants.isnot(None),
                    Recording.participants != ''
                ).all()

                available_speakers = set()
                for recording in recordings_with_participants:
                    if recording.participants:
                        participants = real_speaker_names(recording.participants.split(','))
                        available_speakers.update(participants)
                
                # Check if any speakers are mentioned in the user query but missing from results
                mentioned_speakers = []
                for speaker in available_speakers:
                    if speaker.lower() in user_message.lower():
                        # Check if this speaker appears in current chunk results
                        speaker_in_results = False
                        for chunk, _ in chunk_results:
                            if chunk and (
                                (chunk.speaker_name and speaker.lower() in chunk.speaker_name.lower()) or
                                (chunk.recording and chunk.recording.participants and speaker.lower() in chunk.recording.participants.lower())
                            ):
                                speaker_in_results = True
                                break
                        
                        if not speaker_in_results:
                            mentioned_speakers.append(speaker)
                
                # If we found mentioned speakers not in results, automatically apply speaker filter
                if mentioned_speakers and not data.get('filter_speakers'):  # Only if no speaker filter already applied
                    # Speakers are not named at INFO.
                    app.logger.info(f"Auto-detected {len(mentioned_speakers)} mentioned speaker(s) not in results")
                    app.logger.debug(f"Auto-detected mentioned speakers not in results: {mentioned_speakers}")
                    yield create_status_response('filtering', f'Detected mention of {", ".join(mentioned_speakers)}, applying speaker filter...')
                    
                    # Apply automatic speaker filter
                    auto_filters = filters.copy()
                    auto_filters['speaker_names'] = mentioned_speakers
                    
                    # Re-run semantic search with speaker filter
                    auto_filtered_chunks = []
                    auto_filtered_seen_ids = set()
                    
                    for query in search_queries:
                        with app.app_context():
                            auto_filtered_results = semantic_search_chunks(user_id, query, auto_filters, data.get('context_chunks', 8))
                            app.logger.info(f"Auto-filtered search ({len(query)} characters) with {len(mentioned_speakers)} speakers returned {len(auto_filtered_results)} chunks")
                            app.logger.debug(f"Auto-filtered search speakers: {mentioned_speakers}")
                            app.logger.debug(f"Auto-filtered search for '{query}' returned {len(auto_filtered_results)} chunks")
                        
                        for chunk, similarity in auto_filtered_results:
                            if chunk and chunk.id not in auto_filtered_seen_ids:
                                auto_filtered_chunks.append((chunk, similarity))
                                auto_filtered_seen_ids.add(chunk.id)
                    
                    # If auto-filter found better results, use them
                    if len(auto_filtered_chunks) > 0:
                        auto_filtered_chunks.sort(key=lambda x: x[1], reverse=True)
                        chunk_results = auto_filtered_chunks[:data.get('context_chunks', 8)]
                        app.logger.info(f"Auto speaker filter found {len(chunk_results)} relevant chunks, using filtered results")
                        filters = auto_filters  # Update filters for context building
            
            # Step 3: Evaluate results and re-query if needed
            if len(chunk_results) < 2:  # If we got very few results, try a broader search
                yield create_status_response('requerying', 'Expanding search scope...')
                
                # Try without speaker filter if it was applied
                broader_filters = filters.copy()
                if 'speaker_names' in broader_filters:
                    del broader_filters['speaker_names']
                    app.logger.info("Retrying search without speaker filter...")
                    
                    for query in search_queries:
                        with app.app_context():
                            chunk_results_broader = semantic_search_chunks(user_id, query, broader_filters, 6)
                        for chunk, similarity in chunk_results_broader:
                            if chunk and chunk.id not in seen_chunk_ids:
                                all_chunks.append((chunk, similarity))
                                seen_chunk_ids.add(chunk.id)
                    
                    # Re-sort and limit
                    all_chunks.sort(key=lambda x: x[1], reverse=True)
                    chunk_results = all_chunks[:data.get('context_chunks', 8)]
                    app.logger.info(f"Broader search returned {len(chunk_results)} total chunks")
            
            # Build context from retrieved chunks
            yield create_status_response('contextualizing', 'Building context...')
            
            # Group chunks by recording and organize properly
            recording_chunks = {}
            recording_ids_in_context = set()
            
            for chunk, similarity in chunk_results:
                if not chunk or not chunk.recording:
                    continue
                recording_id = chunk.recording.id
                recording_ids_in_context.add(recording_id)
                
                if recording_id not in recording_chunks:
                    recording_chunks[recording_id] = {
                        'recording': chunk.recording,
                        'chunks': []
                    }
                
                recording_chunks[recording_id]['chunks'].append({
                    'chunk': chunk,
                    'similarity': similarity
                })
            
            # Build organized context pieces
            context_pieces = []
            
            for recording_id, data in recording_chunks.items():
                recording = data['recording']
                chunks = data['chunks']
                
                # Sort chunks by their index to maintain chronological order
                chunks.sort(key=lambda x: x['chunk'].chunk_index)
                
                # Build recording header with complete metadata
                header = f"=== {recording.title} [Recording ID: {recording_id}] ==="
                if recording.meeting_date:
                    header += f" ({to_local(recording.meeting_date, user_zone).strftime('%Y-%m-%d %H:%M')})"
                
                # Add participants information
                if recording.participants:
                    participants_list = [p.strip() for p in recording.participants.split(',') if p.strip()]
                    header += f"\\nParticipants: {', '.join(participants_list)}"
                
                context_piece = header + "\\n\\n"
                
                # Process chunks and detect non-continuity
                prev_chunk_index = None
                for chunk_data in chunks:
                    chunk = chunk_data['chunk']
                    similarity = chunk_data['similarity']
                    
                    # Check for non-continuity
                    if prev_chunk_index is not None and chunk.chunk_index != prev_chunk_index + 1:
                        context_piece += "\\n[... gap in transcript - non-consecutive chunks ...]\\n\\n"
                    
                    # Add speaker information if available
                    speaker_info = ""
                    if chunk.speaker_name:
                        speaker_info = f"{chunk.speaker_name}: "
                    elif chunk.start_time is not None:
                        speaker_info = f"[{chunk.start_time:.1f}s]: "
                    
                    # Add timing info if available
                    timing_info = ""
                    if chunk.start_time is not None and chunk.end_time is not None:
                        timing_info = f" [{chunk.start_time:.1f}s-{chunk.end_time:.1f}s]"
                    
                    context_piece += f"{speaker_info}{chunk.content}{timing_info} (similarity: {similarity:.3f})\\n\\n"
                    prev_chunk_index = chunk.chunk_index
                
                context_pieces.append(context_piece)
            
            app.logger.info(f"Built context from {len(chunk_results)} chunks across {len(recording_chunks)} recordings")
            
            # Generate response
            yield create_status_response('responding', 'Generating response...')
            
            # Prepare system prompt
            language_instruction = f"Please provide all your responses in {user_output_language}." if user_output_language else ""
            
            # Build filter description for context
            filter_description = []
            with app.app_context():
                if data.get('filter_tags'):
                    tag_names = [tag.name for tag in Tag.query.filter(Tag.id.in_(data['filter_tags'])).all()]
                    filter_description.append(f"tags: {', '.join(tag_names)}")
                if data.get('filter_folders'):
                    folder_names = [f.name for f in Folder.query.filter(Folder.id.in_(data['filter_folders'])).all()]
                    filter_description.append(f"folders: {', '.join(folder_names)}")
            if data.get('filter_speakers'):
                filter_description.append(f"speakers: {', '.join(data['filter_speakers'])}")
            if data.get('filter_date_from') or data.get('filter_date_to'):
                date_range = []
                if data.get('filter_date_from'):
                    date_range.append(f"from {data['filter_date_from']}")
                if data.get('filter_date_to'):
                    date_range.append(f"to {data['filter_date_to']}")
                filter_description.append(f"dates: {' '.join(date_range)}")
            
            filter_text = f" (filtered by {'; '.join(filter_description)})" if filter_description else ""
            
            context_text = "\n\n".join(context_pieces) if context_pieces else "No relevant context found."
            
            # Get transcript length limit setting and available speakers
            with app.app_context():
                transcript_limit = SystemSetting.get_setting('transcript_length_limit', 30000)
                
                # Get all available speakers for this user
                recordings_with_participants = Recording.query.filter_by(user_id=user_id).filter(
                    Recording.participants.isnot(None),
                    Recording.participants != ''
                ).all()
                
                available_speakers = set()
                for recording in recordings_with_participants:
                    if recording.participants:
                        participants = real_speaker_names(recording.participants.split(','))
                        available_speakers.update(participants)
                
                available_speakers = sorted(list(available_speakers))
            
            system_prompt = f"""{assistant_identity(assistant_name, user_name, user_title, user_company)} {language_instruction}

You are analyzing transcriptions from multiple recordings{filter_text}. The following context has been retrieved based on semantic similarity to the user's question:

<<start context>>
{context_text}
<<end context>>

The system has automatically analyzed your query and retrieved the most relevant context from your transcriptions. The search returned {len(chunk_results)} chunks from {len(recording_ids_in_context)} recording(s).

**Available speakers in your recordings**: {', '.join(available_speakers) if available_speakers else 'None available'}

**Recording IDs in context**: {list(recording_ids_in_context)}

IMPORTANT FORMATTING INSTRUCTIONS:
You MUST use proper markdown formatting in your responses. Structure your response as follows:

1. **Always use markdown syntax** - Use `#`, `##`, `###` for headings, `**bold**`, `*italic*`, `-` for lists, etc.
2. Start with a brief summary or preamble if helpful
3. Organize information by source transcript using clear markdown headings
4. Use the format: `## [Recording Title] - [Date if available]` 
5. Under each heading, provide the relevant information from that specific recording using bullet points and formatting
6. Include speaker names when referring to specific statements using **bold** formatting
7. Use bullet points (`-`) and sub-bullets for organizing information clearly

**Required Example Structure:**
Brief summary with **key points** highlighted...

## Meeting Discussion on Project Implementation - 2024-06-18
- **Speaker A** mentioned that "there's significant support needed for implementation"
- **Speaker B** confirmed the upcoming meeting with the technical team
- Key topics discussed:
  - Budget planning considerations
  - Timeline coordination needs

## Budget Planning Meeting - 2024-05-30  
- **Speaker A** reviewed the budget document
- **Speaker C** will approve the final version for submission
- Important details:
  - Budget represents approximately 1/3 of the project total
  - Coordination needed for upcoming milestones

Order your response with notes from the most recent meetings first. Always use proper markdown formatting and structure by source recording for maximum clarity and readability."""
    
            # Prepare messages array
            messages = [{"role": "system", "content": system_prompt}]
            if message_history:
                # Only the role and text reach the model: the Ask page keeps
                # its activity and sources on each entry for its own display.
                messages.extend({'role': m['role'], 'content': m['content']} for m in message_history)
            messages.append({"role": "user", "content": user_message})

            # Enable streaming
            stream = call_chat_completion(
                messages=messages,
                temperature=0.7,
                max_tokens=int(os.environ.get("CHAT_MAX_TOKENS", "2000")),
                stream=True,
                user_id=user_id,
                operation_type='chat'
            )

            # Buffer content to detect full transcript requests
            response_buffer = ""

            # Buffer content to detect full transcript requests
            response_buffer = ""
            content_buffer = ""
            in_thinking = False
            thinking_buffer = ""
            
            for chunk in stream:
                if not chunk.choices:
                    continue
                content = chunk.choices[0].delta.content
                if content:
                    response_buffer += content
                    content_buffer += content
                    
                    # Check if this is a full transcript request
                    if response_buffer.strip().startswith("REQUEST_FULL_TRANSCRIPT:"):
                        lines = response_buffer.split('\n')
                        request_line = lines[0].strip()
                        
                        if ':' in request_line:
                            try:
                                recording_id = int(request_line.split(':')[1])
                                app.logger.info(f"Agent requested full transcript for recording {recording_id}")
                                
                                # Fetch full transcript
                                yield create_status_response('fetching', f'Retrieving full transcript for recording {recording_id}...')
                                
                                with app.app_context():
                                    recording = db.session.get(Recording, recording_id)
                                    if recording and recording.user_id == user_id and recording.transcription:
                                        # Apply transcript length limit
                                        if transcript_limit == -1:
                                            full_transcript = recording.transcription
                                        else:
                                            full_transcript = recording.transcription[:transcript_limit]
                                        
                                        # Add full transcript to context
                                        full_context = f"{context_text}\n\n<<FULL TRANSCRIPT - {recording.title}>>\n{full_transcript}\n<<END FULL TRANSCRIPT>>"
                                        
                                        # Update system prompt with full transcript
                                        updated_system_prompt = system_prompt.replace(
                                            f"<<start context>>\n{context_text}\n<<end context>>",
                                            f"<<start context>>\n{full_context}\n<<end context>>"
                                        )
                                        
                                        # Create new messages with updated context
                                        updated_messages = [{"role": "system", "content": updated_system_prompt}]
                                        if message_history:
                                            updated_messages.extend({'role': m['role'], 'content': m['content']} for m in message_history)
                                        updated_messages.append({"role": "user", "content": user_message})
                                        
                                        # Generate new response with full context
                                        yield create_status_response('responding', 'Analyzing full transcript...')

                                        new_stream = call_chat_completion(
                                            messages=updated_messages,
                                            temperature=0.7,
                                            max_tokens=int(os.environ.get("CHAT_MAX_TOKENS", "2000")),
                                            stream=True,
                                            user_id=user_id,
                                            operation_type='chat'
                                        )

                                        # Use helper function to process streaming with thinking tag support
                                        for response in process_streaming_with_thinking(new_stream, user_id=user_id, operation_type='chat', model_name=get_chat_config()['model_name']):
                                            yield response
                                        return
                                    else:
                                        # Recording not found or no permission
                                        error_msg = f"\n\nError: Unable to access full transcript for recording {recording_id}. Recording may not exist or you may not have permission."
                                        yield f"data: {json.dumps({'delta': error_msg})}\n\n"
                                        yield f"data: {json.dumps({'end_of_stream': True})}\n\n"
                                        return
                                        
                            except (ValueError, IndexError):
                                app.logger.warning(f"Invalid transcript request format ({len(request_line)} characters)")
                                app.logger.debug(f"Invalid transcript request: {request_line}")
                                # Continue with normal streaming
                                pass
                    
                    # Process the buffer to detect and handle thinking tags
                    while True:
                        if not in_thinking:
                            # Look for opening thinking tag
                            think_start = re.search(r'<think(?:ing)?>', content_buffer, re.IGNORECASE)
                            if think_start:
                                # Send any content before the thinking tag
                                before_thinking = content_buffer[:think_start.start()]
                                if before_thinking:
                                    yield f"data: {json.dumps({'delta': before_thinking})}\n\n"
                                
                                # Start capturing thinking content
                                in_thinking = True
                                content_buffer = content_buffer[think_start.end():]
                                thinking_buffer = ""
                            else:
                                # No thinking tag found, send accumulated content
                                if content_buffer:
                                    yield f"data: {json.dumps({'delta': content_buffer})}\n\n"
                                content_buffer = ""
                                break
                        else:
                            # We're inside a thinking tag, look for closing tag
                            think_end = re.search(r'</think(?:ing)?>', content_buffer, re.IGNORECASE)
                            if think_end:
                                # Capture thinking content up to the closing tag
                                thinking_buffer += content_buffer[:think_end.start()]
                                
                                # Send the thinking content as a special type
                                if thinking_buffer.strip():
                                    yield f"data: {json.dumps({'thinking': thinking_buffer.strip()})}\n\n"
                                
                                # Continue processing after the closing tag
                                in_thinking = False
                                content_buffer = content_buffer[think_end.end():]
                                thinking_buffer = ""
                            else:
                                # Still inside thinking tag, accumulate content
                                thinking_buffer += content_buffer
                                content_buffer = ""
                                break
            
            # Handle any remaining content
            if in_thinking and thinking_buffer:
                # Unclosed thinking tag - send as thinking content
                yield f"data: {json.dumps({'thinking': thinking_buffer.strip()})}\n\n"
            elif content_buffer:
                # Regular content
                yield f"data: {json.dumps({'delta': content_buffer})}\n\n"
            
            yield f"data: {json.dumps({'end_of_stream': True})}\n\n"

        except TokenBudgetExceeded as e:
            app.logger.warning(f"Token budget exceeded for user {user_id}: {e}")
            yield f"data: {json.dumps({'error': str(e), 'budget_exceeded': True})}\n\n"
        except Exception as e:
            log_failure(app.logger, 'error', f"Error in enhanced chat generation for user {user_id}", e)
            yield f"data: {json.dumps({'error': user_error_text(e, extra_sources=[user_message] if isinstance(user_message, str) else ())})}\n\n"
        finally:
            ctx.pop()

    # Agentic mode (opt-in beta, ENABLE_INQUIRE_AGENT): a ReAct-style tool
    # loop replaces the single-shot pipeline. The legacy generator is
    # passed as fallback so a failing agent never breaks Inquire.
    from src.services.inquire_agent import agent_enabled, run_inquire_agent
    if mode != 'rag' and agent_enabled():
        user_ctx = {
            'name': user_name,
            'title': user_title,
            'company': user_company,
            'output_language': user_output_language,
            'timezone': user_zone,
            'assistant_name': assistant_name,
            'language': ui_language,
        }
        agent_gen = run_inquire_agent(
            app, user_id, user_ctx, user_message, message_history,
            data.get('conversation_memory'), filters,
            legacy_fallback=generate_enhanced_chat,
            tool_names=tool_names,
        )
        return agent_gen

    return generate_enhanced_chat()


@inquire_bp.route('/api/inquire/chat', methods=['POST'])
@login_required
def inquire_chat():
    """Chat with filtered transcriptions using RAG."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify(api_error('inquire_disabled')), 403
    try:
        data, body_error = json_object_body()
        if body_error:
            return body_error
        if not data:
            return jsonify(api_error('no_data_provided')), 400
        
        # Stripped; blank or holding NUL is no question (#670)
        user_message, message_error = checked_question(data.get('message'), api_error('no_message_provided'))
        if message_error:
            return jsonify(message_error), 400
        data = dict(data, message=user_message)
        # A list of {role, content}, else 400; the newest turns within the
        # limits reach the model (src/utils/assistant_limits.py, #633).
        raw_history = data.get('message_history')
        message_history, bad_history = checked_history(raw_history)
        if bad_history:
            return jsonify(bad_history), 400
        data = dict(data, message_history=message_history)
        
        # Check if a chat model is available, resolved for this request so an
        # assignment made in System Settings applies without a restart.
        if not llm_config.is_configured('chat'):
            return jsonify(api_error('chat_openrouter_not_configured')), 503
        
        # One event stream for both engines: retrieval stages arrive as
        # steps too, labelled in the user's language (assistant_events).
        from src.services.assistant_events import filters_from_request, library_events, tools_for_surface
        from src.utils.localization import request_language
        # A filter that cannot be used is refused, never dropped
        # (the stream would otherwise fail after it starts).
        try:
            filters_from_request(data)
        except FilterError as e:
            return jsonify(api_error(e.code)), 400
        # context_chunks bounds the chunks given to the model: a whole number
        # from 1 to 50, like top_k of the search route (#628).
        context_chunks = data.get('context_chunks', 8)
        if isinstance(context_chunks, bool) or not isinstance(context_chunks, int):
            return jsonify(api_error('invalid_integer_value')), 400
        if not 1 <= context_chunks <= INQUIRE_SEARCH_MAX_TOP_K:
            return jsonify(api_error('invalid_context_chunks', max=INQUIRE_SEARCH_MAX_TOP_K)), 400
        stream = inquire_stream(current_user, data, tool_names=tools_for_surface('ask'))
        trimmed = history_trimmed_event(raw_history, message_history, request_language())

        def with_notice():
            if trimmed:
                yield trimmed
            yield from library_events(stream, request_language())
        return Response(with_notice(), mimetype='text/event-stream')

    except Exception as e:
        log_failure(current_app.logger, 'error', "Error in inquire chat endpoint", e)
        return jsonify({'error': user_error_text(e)}), 500


# Filter groups in the order the downloaded file lists them
_INQUIRE_FILTER_GROUPS = (
    ('tags', 'Tags'),
    ('folders', 'Folders'),
    ('speakers', 'Speakers'),
    ('recordings', 'Recordings'),
    ('dates', 'Dates'),
)


def _inquire_filter_line(filters):
    """The filter names as 'Label: a, b' groups joined by '; ', or '' when none are set."""
    if not isinstance(filters, dict):
        return ''
    parts = []
    for key, label in _INQUIRE_FILTER_GROUPS:
        value = filters.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            continue
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if items:
            parts.append(f'{label}: {", ".join(items)}')
    return '; '.join(parts)


# Limits on the Sources lists the page sends with a download
_MAX_SOURCES_PER_TURN = 50
_MAX_SOURCES_TOTAL = 200
_MAX_SOURCE_TITLE = 300
# A source links to a recording page, optionally at a time in seconds
_SOURCE_URL = re.compile(r'/recordings/([0-9]+)(?:\?t=[0-9]+)?')


def _inquire_turn_sources(raw, budget):
    """(number, title, url) for each valid source in raw, at most budget of them.

    The number is the one the page showed (`n`; numbers continue through a
    chat, Ambrose 2b), else the source's position in the list, so the numbers
    stay those of the page when an entry is dropped. An entry is kept
    only when its title is a non-empty string and its url is a relative
    /recordings/<id> path with an optional ?t=<seconds>.
    """
    if not isinstance(raw, list) or budget <= 0:
        return []
    sources = []
    for number, item in enumerate(raw[:_MAX_SOURCES_PER_TURN], start=1):
        if len(sources) >= budget:
            break
        if not isinstance(item, dict):
            continue
        title, url = item.get('title'), item.get('url')
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        title = ' '.join(title.split())[:_MAX_SOURCE_TITLE]
        if not title or not _SOURCE_URL.fullmatch(url):
            continue
        shown = item.get('n')
        if isinstance(shown, int) and not isinstance(shown, bool) and 0 < shown <= 10000:
            number = shown
        sources.append((number, title, url))
    return sources


# A citation the agent writes into an answer: [Title @ m:ss](/recordings/<id>?t=<s>)
_CITATION_LINK = re.compile(r'\[([^\]]+)\]\((/recordings/([0-9]+)(?:\?t=([0-9]+))?)\)')
_CITATION_TIME = re.compile(r'@\s*([0-9]+(?::[0-9]{1,2}){0,2})\s*$')


def _format_seconds(seconds):
    hours, rest = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


def _number_citations(content, sources):
    """Citation links in content relabelled "<n> @ m:ss" as the page shows them.

    n is the number of the cited recording in sources; a link to a recording
    that is not in sources keeps its own text. The time comes from the
    link's ?t= value, else from an "@ m:ss" at the end of its text.
    """
    numbers = {}
    for number, _, url in sources:
        numbers.setdefault(_SOURCE_URL.fullmatch(url).group(1), number)

    def relabel(match):
        number = numbers.get(match.group(3))
        if number is None:
            return match.group(0)
        if match.group(4) is not None:
            seconds = int(match.group(4))
        else:
            stated = _CITATION_TIME.search(match.group(1))
            seconds = None
            if stated:
                seconds = 0
                for part in stated.group(1).split(':'):
                    seconds = seconds * 60 + int(part)
        label = f'{number} @ {_format_seconds(seconds)}' if seconds is not None else str(number)
        return f'[{label}]({match.group(2)})'

    return _CITATION_LINK.sub(relabel, content) if numbers else content


def _inquire_download_turns(messages):
    """(role, content, sources) for each turn with non-empty string content.

    Sources are read for assistant turns only (see _inquire_turn_sources), and
    the citations in such a turn are numbered to match them; other fields are
    ignored.
    """
    if not isinstance(messages, list):
        return []
    turns = []
    budget = _MAX_SOURCES_TOTAL
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if not isinstance(content, str) or content == '':
            continue
        role = message.get('role')
        if not isinstance(role, str) or role == '':
            role = 'unknown'
        sources = _inquire_turn_sources(message.get('sources'), budget) if role == 'assistant' else []
        budget -= len(sources)
        turns.append((role, _number_citations(content, sources), sources))
    return turns


def _response_scope(data):
    """True when the body asks for one answer ("scope": "response").

    The scope only chooses the filename. Any other value, a non-string
    included, counts as the whole conversation.
    """
    scope = data.get('scope')
    return isinstance(scope, str) and scope == 'response'


@inquire_bp.route('/api/inquire/download', methods=['POST'])
@login_required
def download_inquire_conversation():
    """Download the Inquire conversation on screen as a Word document."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify(api_error('inquire_disabled')), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'messages' not in data:
        return jsonify(api_error('no_messages_provided')), 400

    turns = _inquire_download_turns(data['messages'])
    if not turns:
        return jsonify(api_error('no_messages_to_download')), 400

    try:
        from src.services.document import (
            add_document_header, append_conversation_turn, docx_attachment_response, link_base_url,
            new_document,
        )
        from src.utils.timezones import now_local

        metadata = [f'Export Date: {now_local(user_timezone(current_user)).strftime("%Y-%m-%d %H:%M")}']
        filter_line = _inquire_filter_line(data.get('filters'))
        if filter_line:
            metadata.append(filter_line)
        doc = new_document()
        from src.utils.assistant_name import assistant_name_of
        add_document_header(doc, translate('inquire.title', name=assistant_name_of(current_user)), metadata)

        base_url = link_base_url()
        for role, content, sources in turns:
            append_conversation_turn(doc, role, content, base_url=base_url, sources=sources)

        filename = 'inquire-response.docx' if _response_scope(data) else 'inquire.docx'
        return docx_attachment_response(doc, filename, filename)

    except Exception as e:
        current_app.logger.error(f"Error generating inquire Word document: {e}")
        return jsonify(api_error('word_document_failed')), 500



@inquire_bp.route('/api/inquire/available_filters', methods=['GET'])
@login_required
def get_available_filters():
    """Get available filter options for the user (includes shared recordings)."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify(api_error('inquire_disabled')), 403
    try:
        # Get user's personal tags
        user_tags = Tag.query.filter_by(user_id=current_user.id, group_id=None).all()

        # Get group tags from user's teams
        group_tags = []
        memberships = GroupMembership.query.filter_by(user_id=current_user.id).all()
        group_ids = [m.group_id for m in memberships]
        if group_ids:
            group_tags = Tag.query.filter(Tag.group_id.in_(group_ids)).all()

        # Combine all tags
        all_tags = user_tags + group_tags

        # Get all accessible recording IDs (own + shared)
        accessible_recording_ids = get_accessible_recording_ids(current_user.id)

        # Get unique speakers from accessible recordings' participants field
        recordings_with_participants = Recording.query.filter(
            Recording.id.in_(accessible_recording_ids),
            Recording.participants.isnot(None),
            Recording.participants != ''
        ).all()

        speaker_names = set()
        for recording in recordings_with_participants:
            if recording.participants:
                # Split participants by comma and clean up; diarization
                # labels (SPEAKER_00) are never offered as a filter
                participants = real_speaker_names(recording.participants.split(','))
                speaker_names.update(participants)

        speaker_names = sorted(list(speaker_names))

        # Get accessible recordings for recording-specific filtering
        recordings = Recording.query.filter(
            Recording.id.in_(accessible_recording_ids),
            Recording.status == 'COMPLETED'
        ).order_by(Recording.created_at.desc()).all()

        # Folders (#428): the user's own and those of their groups, when the
        # feature is on
        folders = []
        if SystemSetting.get_setting('enable_folders', False):
            folders = Folder.query.filter_by(user_id=current_user.id, group_id=None).all()
            if group_ids:
                folders += Folder.query.filter(Folder.group_id.in_(group_ids)).all()
            folders.sort(key=lambda f: f.name.lower())

        return jsonify({
            'tags': [tag.to_dict() for tag in all_tags],
            'folders': [{'id': f.id, 'name': f.name, 'color': f.color,
                         'group_name': f.group.name if f.group else None,
                         'recording_count': len(f.recordings) if f.recordings else 0} for f in folders],
            'speakers': speaker_names,
            'recordings': [{'id': r.id, 'title': r.title, 'meeting_date': r.meeting_date.isoformat() if r.meeting_date else None} for r in recordings]
        })

    except Exception as e:
        current_app.logger.error(f"Error getting available filters: {e}")
        return internal_error(e)


from src.utils.system_settings import apply_hot_settings
_SYSTEM_SETTING_MAP = {
    "ENABLE_AUTO_DELETION": ("enable_auto_deletion", "ENABLE_AUTO_DELETION", False),
    "USERS_CAN_DELETE": ("users_can_delete", "USERS_CAN_DELETE", True),
    "ENABLE_INTERNAL_SHARING": ("enable_internal_sharing", "ENABLE_INTERNAL_SHARING", False),
}
_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)
