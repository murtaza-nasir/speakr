#!/usr/bin/env python3
"""
ASR JSON Parser - Analyzes speaker information in ASR response JSON files
"""

import json
import sys
from collections import defaultdict, Counter

def analyze_asr_json(json_data):
    """
    Analyze ASR JSON data to understand speaker distribution and identify issues
    """
    if not isinstance(json_data, dict) or 'segments' not in json_data:
        print("ERROR: Invalid JSON structure. Expected dict with 'segments' key.")
        return
    
    segments = json_data['segments']
    if not isinstance(segments, list):
        print("ERROR: 'segments' should be a list.")
        return
    
    print(f"=== ASR JSON Analysis ===")
    print(f"Total segments: {len(segments)}")
    print()
    
    # Track segment-level speakers
    segment_speakers = []
    segments_with_speaker = 0
    segments_without_speaker = 0
    
    # Track word-level speakers
    word_speakers = []
    words_with_speaker = 0
    words_without_speaker = 0
    
    # Track segments with null speakers
    null_speaker_segments = []
    
    for i, segment in enumerate(segments):
        # Analyze segment-level speaker
        segment_speaker = segment.get('speaker')
        if segment_speaker is not None:
            segment_speakers.append(segment_speaker)
            segments_with_speaker += 1
        else:
            segments_without_speaker += 1
            null_speaker_segments.append(i)
        
        # Analyze word-level speakers
        words = segment.get('words', [])
        for word_data in words:
            word_speaker = word_data.get('speaker')
            if word_speaker is not None:
                word_speakers.append(word_speaker)
                words_with_speaker += 1
            else:
                words_without_speaker += 1
    
    # Print segment-level analysis
    print("=== SEGMENT-LEVEL SPEAKERS ===")
    print(f"Segments with speakers: {segments_with_speaker}")
    print(f"Segments without speakers: {segments_without_speaker}")
    
    if segment_speakers:
        segment_speaker_counts = Counter(segment_speakers)
        print(f"Unique segment speakers: {sorted(segment_speaker_counts.keys())}")
        print("Segment speaker distribution:")
        for speaker, count in segment_speaker_counts.most_common():
            print(f"  {speaker}: {count} segments")
    else:
        print("No segment-level speakers found!")
    
    print()
    
    # Print word-level analysis
    print("=== WORD-LEVEL SPEAKERS ===")
    print(f"Words with speakers: {words_with_speaker}")
    print(f"Words without speakers: {words_without_speaker}")
    
    if word_speakers:
        word_speaker_counts = Counter(word_speakers)
        print(f"Unique word speakers: {sorted(word_speaker_counts.keys())}")
        print("Word speaker distribution:")
        for speaker, count in word_speaker_counts.most_common():
            print(f"  {speaker}: {count} words")
    else:
        print("No word-level speakers found!")
    
    print()
    
    # Analyze segments without speakers
    if null_speaker_segments:
        print("=== SEGMENTS WITHOUT SPEAKERS ===")
        print(f"Segment indices without speakers: {null_speaker_segments[:10]}{'...' if len(null_speaker_segments) > 10 else ''}")
        
        print("\nFirst few segments without speakers:")
        for i in null_speaker_segments[:5]:
            segment = segments[i]
            text = segment.get('text', '').strip()
            start = segment.get('start')
            end = segment.get('end')
            words = segment.get('words', [])
            
            print(f"  Segment {i}: '{text}' ({start}-{end}s)")
            print(f"    Keys: {list(segment.keys())}")
            
            # Check if words have speakers even when segment doesn't
            word_speakers_in_segment = [w.get('speaker') for w in words if w.get('speaker')]
            if word_speakers_in_segment:
                word_speaker_counts = Counter(word_speakers_in_segment)
                print(f"    Word speakers: {dict(word_speaker_counts)}")
            else:
                print(f"    No word speakers either")
            print()
    
    # Suggest speaker assignment strategy
    print("=== SPEAKER ASSIGNMENT STRATEGY ===")
    if segments_without_speaker > 0:
        print(f"Found {segments_without_speaker} segments without speakers.")
        
        if words_with_speaker > 0:
            print("RECOMMENDATION: Use word-level speaker information to assign segment speakers.")
            print("Strategy: For segments without speakers, find the most common speaker among their words.")
        else:
            print("RECOMMENDATION: Assign a default speaker label (e.g., 'UNKNOWN_SPEAKER') to segments without speakers.")
    else:
        print("All segments have speakers assigned. No action needed.")

def suggest_preprocessing_fix(json_data):
    """
    Suggest how to fix the preprocessing based on the JSON structure
    """
    print("\n=== PREPROCESSING FIX SUGGESTION ===")
    
    segments = json_data.get('segments', [])
    if not segments:
        return
    
    # Check if we can derive segment speakers from word speakers
    segments_fixable = 0
    for segment in segments:
        if segment.get('speaker') is None:
            words = segment.get('words', [])
            word_speakers = [w.get('speaker') for w in words if w.get('speaker')]
            if word_speakers:
                segments_fixable += 1
    
    if segments_fixable > 0:
        print(f"✅ {segments_fixable} segments can be fixed using word-level speaker information.")
        print("\nSuggested code fix:")
        print("""
# In the ASR processing function, replace the segment processing with:
for i, segment in enumerate(asr_response_data['segments']):
    speaker = segment.get('speaker')
    text = segment.get('text', '').strip()
    
    # If segment doesn't have a speaker, try to derive from words
    if speaker is None:
        words = segment.get('words', [])
        word_speakers = [w.get('speaker') for w in words if w.get('speaker')]
        if word_speakers:
            # Use the most common speaker among the words
            from collections import Counter
            speaker_counts = Counter(word_speakers)
            speaker = speaker_counts.most_common(1)[0][0]
            app.logger.info(f"Derived speaker '{speaker}' for segment {i} from word-level data")
        else:
            speaker = 'UNKNOWN_SPEAKER'
            app.logger.warning(f"No speaker info available for segment {i}, using UNKNOWN_SPEAKER")
    
    simplified_segments.append({
        'speaker': speaker,
        'sentence': text,
        'start_time': segment.get('start'),
        'end_time': segment.get('end')
    })
""")
    else:
        print("❌ Segments cannot be fixed using word-level data.")
        print("Recommendation: Assign 'UNKNOWN_SPEAKER' to segments without speakers.")

def main():
    if len(sys.argv) != 2:
        print("Usage: python parse_asr_json.py <json_file>")
        print("   or: python parse_asr_json.py -")
        print("       (use '-' to read from stdin)")
        sys.exit(1)
    
    filename = sys.argv[1]
    
    try:
        if filename == '-':
            # Read from stdin
            json_text = sys.stdin.read()
        else:
            # Read from file
            with open(filename, 'r', encoding='utf-8') as f:
                json_text = f.read()
        
        # Parse JSON
        json_data = json.loads(json_text)
        
        # Analyze the data
        analyze_asr_json(json_data)
        suggest_preprocessing_fix(json_data)
        
    except FileNotFoundError:
        print(f"ERROR: File '{filename}' not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON - {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
