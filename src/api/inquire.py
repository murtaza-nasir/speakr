"""
Semantic search and chat functionality.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
import re
import time
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from src.database import db
from src.models import *
from src.utils import *
from src.services.embeddings import get_accessible_recording_ids, semantic_search_chunks, EMBEDDINGS_AVAILABLE
from src.services.llm import call_llm_completion, call_chat_completion, process_streaming_with_thinking, client, chat_client, TokenBudgetExceeded

# Create blueprint
inquire_bp = Blueprint('inquire', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'

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
@login_required
def inquire():
    # Check if inquire mode is enabled
    if not ENABLE_INQUIRE_MODE:
        flash('Inquire mode is not enabled on this server.', 'warning')
        return redirect(url_for('recordings.index'))

    # Check if user is a group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    # Render the inquire page with user context for theming
    return render_template('inquire.html',
                         use_asr_endpoint=USE_ASR_ENDPOINT,
                         current_user=current_user,
                         is_team_admin=is_team_admin)



@inquire_bp.route('/api/inquire/sessions', methods=['GET'])
@login_required
def get_inquire_sessions():
    """Get all inquire sessions for the current user."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify({'error': 'Inquire mode is not enabled'}), 403
    try:
        sessions = InquireSession.query.filter_by(user_id=current_user.id).order_by(InquireSession.last_used.desc()).all()
        return jsonify([session.to_dict() for session in sessions])
    except Exception as e:
        current_app.logger.error(f"Error getting inquire sessions: {e}")
        return jsonify({'error': str(e)}), 500



@inquire_bp.route('/api/inquire/sessions', methods=['POST'])
@login_required
def create_inquire_session():
    """Create a new inquire session with filters."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify({'error': 'Inquire mode is not enabled'}), 403
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        session = InquireSession(
            user_id=current_user.id,
            session_name=data.get('session_name'),
            filter_tags=json.dumps(data.get('filter_tags', [])),
            filter_speakers=json.dumps(data.get('filter_speakers', [])),
            filter_date_from=datetime.fromisoformat(data['filter_date_from']).date() if data.get('filter_date_from') else None,
            filter_date_to=datetime.fromisoformat(data['filter_date_to']).date() if data.get('filter_date_to') else None,
            filter_recording_ids=json.dumps(data.get('filter_recording_ids', []))
        )
        
        db.session.add(session)
        db.session.commit()
        
        return jsonify(session.to_dict()), 201
        
    except Exception as e:
        current_app.logger.error(f"Error creating inquire session: {e}")
        return jsonify({'error': str(e)}), 500



@inquire_bp.route('/api/inquire/search', methods=['POST'])
@login_required
def inquire_search():
    """Perform semantic search within filtered transcriptions."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify({'error': 'Inquire mode is not enabled'}), 403
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        query = data.get('query')
        if not query:
            return jsonify({'error': 'No query provided'}), 400
        
        # Build filters from request
        filters = {}
        if data.get('filter_tags'):
            filters['tag_ids'] = data['filter_tags']
        if data.get('filter_speakers'):
            filters['speaker_names'] = data['filter_speakers']
        if data.get('filter_recording_ids'):
            filters['recording_ids'] = data['filter_recording_ids']
        if data.get('filter_date_from'):
            filters['date_from'] = datetime.fromisoformat(data['filter_date_from']).date()
        if data.get('filter_date_to'):
            filters['date_to'] = datetime.fromisoformat(data['filter_date_to']).date()
        
        # Perform semantic search
        top_k = data.get('top_k', 5)
        chunk_results = semantic_search_chunks(current_user.id, query, filters, top_k)
        
        # Format results
        results = []
        for chunk, similarity in chunk_results:
            result = chunk.to_dict()
            result['similarity'] = similarity
            result['recording_title'] = chunk.recording.title
            result['recording_meeting_date'] = local_datetime_filter(chunk.recording.meeting_date)
            results.append(result)
        
        return jsonify({'results': results})
        
    except Exception as e:
        current_app.logger.error(f"Error in inquire search: {e}")
        return jsonify({'error': str(e)}), 500



@inquire_bp.route('/api/inquire/chat', methods=['POST'])
@login_required
def inquire_chat():
    """Chat with filtered transcriptions using RAG."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify({'error': 'Inquire mode is not enabled'}), 403
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        user_message = data.get('message')
        message_history = data.get('message_history', [])
        
        if not user_message:
            return jsonify({'error': 'No message provided'}), 400
        
        # Check if OpenRouter client is available
        if client is None:
            return jsonify({'error': 'Chat service is not available (OpenRouter client not configured)'}), 503
        
        # Build filters from request
        filters = {}
        if data.get('filter_tags'):
            filters['tag_ids'] = data['filter_tags']
        if data.get('filter_speakers'):
            filters['speaker_names'] = data['filter_speakers']
        if data.get('filter_recording_ids'):
            filters['recording_ids'] = data['filter_recording_ids']
        if data.get('filter_date_from'):
            filters['date_from'] = datetime.fromisoformat(data['filter_date_from']).date()
        if data.get('filter_date_to'):
            filters['date_to'] = datetime.fromisoformat(data['filter_date_to']).date()
        
        # Debug logging
        current_app.logger.info(f"Inquire chat - User: {current_user.username}, Query: '{user_message}', Filters: {filters}")
        
        # Capture user context and app before generator to avoid context issues
        user_id = current_user.id
        user_name = current_user.name if current_user.name else "the user"
        user_title = current_user.job_title if current_user.job_title else "professional"
        user_company = current_user.company if current_user.company else "their organization"
        user_output_language = current_user.output_language if current_user.output_language else None
        app = current_app._get_current_object()  # Capture app for use in generator
        
        # Enhanced query processing with enrichment and debugging
        def create_status_response(status, message):
            """Helper to create SSE status updates"""
            return f"data: {json.dumps({'status': status, 'message': message})}\n\n"
        
        def generate_enhanced_chat():
            # Explicitly reference outer scope variables
            nonlocal user_id, user_name, user_title, user_company, user_output_language, data, filters

            # Push app context for entire generator execution
            # This is needed because call_llm_completion uses current_app.logger internally
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
                    router_response = call_llm_completion(
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
                    app.logger.info(f"Router decision: {route_decision}")

                    if route_decision == "DIRECT":
                        # Direct response without RAG lookup
                        yield create_status_response('responding', 'Generating direct response...')

                        direct_prompt = f"""You are assisting {user_name}. Respond to their request directly using proper markdown formatting.

User request: "{user_message}"

Previous conversation context (if relevant):
{json.dumps(message_history[-2:] if message_history else [])}

Use proper markdown formatting including headings (##), bold (**text**), bullet points (-), etc."""

                        stream = call_llm_completion(
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
                        for response in process_streaming_with_thinking(stream, user_id=user_id, operation_type='chat', model_name=os.environ.get('LLM_MODEL')):
                            yield response
                        return
                        
                except Exception as e:
                    app.logger.warning(f"Router failed, defaulting to RAG: {e}")
                
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
                    enrichment_response = call_llm_completion(
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
                            app.logger.warning(f"Query enrichment response not JSON: {content[:200]}")
                            raise ValueError("No JSON array found in response")

                    app.logger.info(f"Enriched search terms: {enriched_terms}")
                    
                    # Combine original query with enriched terms for search
                    search_queries = [user_message] + enriched_terms[:3]  # Use original + top 3 enriched terms
                    
                except Exception as e:
                    app.logger.warning(f"Query enrichment failed, using original query: {e}")
                    search_queries = [user_message]
                
                # Step 2: Semantic search with multiple queries
                yield create_status_response('searching', 'Searching transcriptions...')
                
                all_chunks = []
                seen_chunk_ids = set()
                
                for query in search_queries:
                    with app.app_context():
                        chunk_results = semantic_search_chunks(user_id, query, filters, 8)
                        app.logger.info(f"Search query '{query}' returned {len(chunk_results)} chunks")
                    
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
                            participants = [p.strip() for p in recording.participants.split(',') if p.strip()]
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
                        app.logger.info(f"Auto-detected mentioned speakers not in results: {mentioned_speakers}")
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
                                app.logger.info(f"Auto-filtered search for '{query}' with speakers {mentioned_speakers} returned {len(auto_filtered_results)} chunks")
                            
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
                        header += f" ({recording.meeting_date})"
                    
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
                            participants = [p.strip() for p in recording.participants.split(',') if p.strip()]
                            available_speakers.update(participants)
                    
                    available_speakers = sorted(list(available_speakers))
                
                system_prompt = f"""You are a professional meeting and audio transcription analyst assisting {user_name}, who is a(n) {user_title} at {user_company}. {language_instruction}

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
                    messages.extend(message_history)
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
                                                updated_messages.extend(message_history)
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
                                            for response in process_streaming_with_thinking(new_stream, user_id=user_id, operation_type='chat', model_name=os.environ.get('CHAT_MODEL')):
                                                yield response
                                            return
                                        else:
                                            # Recording not found or no permission
                                            error_msg = f"\n\nError: Unable to access full transcript for recording {recording_id}. Recording may not exist or you may not have permission."
                                            yield f"data: {json.dumps({'delta': error_msg})}\n\n"
                                            yield f"data: {json.dumps({'end_of_stream': True})}\n\n"
                                            return
                                            
                                except (ValueError, IndexError):
                                    app.logger.warning(f"Invalid transcript request format: {request_line}")
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
                app.logger.error(f"Error in enhanced chat generation: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            finally:
                ctx.pop()

        return Response(generate_enhanced_chat(), mimetype='text/event-stream')
        
    except Exception as e:
        current_app.logger.error(f"Error in inquire chat endpoint: {str(e)}")
        return jsonify({'error': str(e)}), 500



@inquire_bp.route('/api/inquire/available_filters', methods=['GET'])
@login_required
def get_available_filters():
    """Get available filter options for the user (includes shared recordings)."""
    if not ENABLE_INQUIRE_MODE:
        return jsonify({'error': 'Inquire mode is not enabled'}), 403
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
                # Split participants by comma and clean up
                participants = [p.strip() for p in recording.participants.split(',') if p.strip()]
                speaker_names.update(participants)

        speaker_names = sorted(list(speaker_names))

        # Get accessible recordings for recording-specific filtering
        recordings = Recording.query.filter(
            Recording.id.in_(accessible_recording_ids),
            Recording.status == 'COMPLETED'
        ).order_by(Recording.created_at.desc()).all()

        return jsonify({
            'tags': [tag.to_dict() for tag in all_tags],
            'speakers': speaker_names,
            'recordings': [{'id': r.id, 'title': r.title, 'meeting_date': f"{r.meeting_date.isoformat()}T00:00:00" if r.meeting_date else None} for r in recordings]
        })

    except Exception as e:
        current_app.logger.error(f"Error getting available filters: {e}")
        return jsonify({'error': str(e)}), 500



