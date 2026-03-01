#!/bin/bash
# test_hotwords.sh - Test hotwords and initial_prompt features
#
# Usage:
#   ./tests/test_hotwords.sh <asr_url> <audio_file>
#   ./tests/test_hotwords.sh http://localhost:9000 temp/Recording\ 4.flac
#
# The audio should contain domain-specific words that Whisper tends to
# misspell (brand names, acronyms, unusual proper nouns). The script runs
# three transcriptions and compares the results.

set -euo pipefail

ASR_URL="${1:-http://localhost:9000}"
AUDIO_FILE="${2:-temp/Recording 4.flac}"
OUTPUT_DIR="temp/hotwords_test_results"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configurable test values - adjust these for your audio
HOTWORDS="Speakr,CTranslate2,PyAnnote,WhisperX"
INITIAL_PROMPT="This is a meeting about AI-powered audio transcription tools including Speakr, CTranslate2, and PyAnnote."

echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  Hotwords & Initial Prompt Test Suite${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""
echo -e "ASR URL:       ${YELLOW}${ASR_URL}${NC}"
echo -e "Audio file:    ${YELLOW}${AUDIO_FILE}${NC}"
echo -e "Hotwords:      ${YELLOW}${HOTWORDS}${NC}"
echo -e "Initial prompt: ${YELLOW}${INITIAL_PROMPT}${NC}"
echo ""

# Verify audio file exists
if [ ! -f "$AUDIO_FILE" ]; then
    echo -e "${RED}ERROR: Audio file not found: ${AUDIO_FILE}${NC}"
    exit 1
fi

# Verify ASR endpoint is reachable
echo -n "Checking ASR endpoint... "
if curl -sf "${ASR_URL}/" > /dev/null 2>&1 || curl -sf "${ASR_URL}/health" > /dev/null 2>&1; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}FAILED${NC}"
    echo "Cannot reach ASR endpoint at ${ASR_URL}"
    exit 1
fi

# Create output directory
mkdir -p "$OUTPUT_DIR"

# ==============================================================
# Test 1: Baseline (no hints)
# ==============================================================
echo ""
echo -e "${CYAN}--- Test 1: Baseline (no hotwords, no initial_prompt) ---${NC}"
echo -n "Transcribing... "

BASELINE_FILE="$OUTPUT_DIR/1_baseline.json"
curl -sS -X POST "${ASR_URL}/asr?output=json&task=transcribe" \
    -F "audio_file=@${AUDIO_FILE}" \
    -o "$BASELINE_FILE"

BASELINE_TEXT=$(python3 -c "
import json
d=json.load(open('$BASELINE_FILE'))
t=d.get('text','')
if isinstance(t, list):
    t=' '.join(seg.get('text','') for seg in t)
print(t[:500])
" 2>/dev/null || echo "PARSE_ERROR")
echo -e "${GREEN}Done${NC}"
echo -e "Preview: ${BASELINE_TEXT:0:200}..."
echo ""

# ==============================================================
# Test 2: With hotwords only
# ==============================================================
echo -e "${CYAN}--- Test 2: With hotwords ---${NC}"
echo -n "Transcribing... "

HOTWORDS_FILE="$OUTPUT_DIR/2_with_hotwords.json"
curl -sS -X POST "${ASR_URL}/asr?output=json&task=transcribe&hotwords=${HOTWORDS}" \
    -F "audio_file=@${AUDIO_FILE}" \
    -o "$HOTWORDS_FILE"

HOTWORDS_TEXT=$(python3 -c "
import json
d=json.load(open('$HOTWORDS_FILE'))
t=d.get('text','')
if isinstance(t, list):
    t=' '.join(seg.get('text','') for seg in t)
print(t[:500])
" 2>/dev/null || echo "PARSE_ERROR")
echo -e "${GREEN}Done${NC}"
echo -e "Preview: ${HOTWORDS_TEXT:0:200}..."
echo ""

# ==============================================================
# Test 3: With hotwords + initial_prompt
# ==============================================================
echo -e "${CYAN}--- Test 3: With hotwords + initial_prompt ---${NC}"
echo -n "Transcribing... "

BOTH_FILE="$OUTPUT_DIR/3_with_both.json"
ENCODED_PROMPT=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$INITIAL_PROMPT'))")
curl -sS -X POST "${ASR_URL}/asr?output=json&task=transcribe&hotwords=${HOTWORDS}&initial_prompt=${ENCODED_PROMPT}" \
    -F "audio_file=@${AUDIO_FILE}" \
    -o "$BOTH_FILE"

BOTH_TEXT=$(python3 -c "
import json
d=json.load(open('$BOTH_FILE'))
t=d.get('text','')
if isinstance(t, list):
    t=' '.join(seg.get('text','') for seg in t)
print(t[:500])
" 2>/dev/null || echo "PARSE_ERROR")
echo -e "${GREEN}Done${NC}"
echo -e "Preview: ${BOTH_TEXT:0:200}..."
echo ""

# ==============================================================
# Test 4: With initial_prompt only
# ==============================================================
echo -e "${CYAN}--- Test 4: With initial_prompt only ---${NC}"
echo -n "Transcribing... "

PROMPT_FILE="$OUTPUT_DIR/4_with_initial_prompt.json"
curl -sS -X POST "${ASR_URL}/asr?output=json&task=transcribe&initial_prompt=${ENCODED_PROMPT}" \
    -F "audio_file=@${AUDIO_FILE}" \
    -o "$PROMPT_FILE"

PROMPT_TEXT=$(python3 -c "
import json
d=json.load(open('$PROMPT_FILE'))
t=d.get('text','')
if isinstance(t, list):
    t=' '.join(seg.get('text','') for seg in t)
print(t[:500])
" 2>/dev/null || echo "PARSE_ERROR")
echo -e "${GREEN}Done${NC}"
echo -e "Preview: ${PROMPT_TEXT:0:200}..."
echo ""

# ==============================================================
# Comparison
# ==============================================================
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  Comparison Results${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# Check if hotwords appear in outputs
python3 << 'PYEOF'
import json
import os

def extract_text(data):
    """Extract full text from ASR response, handling both string and segment list formats."""
    text = data.get("text", "")
    if isinstance(text, list):
        return " ".join(seg.get("text", "") for seg in text)
    return text

hotwords = ["Speakr", "CTranslate2", "PyAnnote", "WhisperX"]
output_dir = os.environ.get("OUTPUT_DIR", "temp/hotwords_test_results")
test_files = {
    "1. Baseline":            f"{output_dir}/1_baseline.json",
    "2. Hotwords only":       f"{output_dir}/2_with_hotwords.json",
    "3. Hotwords + prompt":   f"{output_dir}/3_with_both.json",
    "4. Initial prompt only": f"{output_dir}/4_with_initial_prompt.json",
}

print(f"{'Test':<25} | {'Hotword Matches':<20} | {'Words Found'}")
print("-" * 75)

for label, filepath in test_files.items():
    try:
        with open(filepath) as f:
            data = json.load(f)
        text = extract_text(data)
        found = []
        for hw in hotwords:
            if hw.lower() in text.lower():
                found.append(hw)
        match_str = f"{len(found)}/{len(hotwords)}"
        found_str = ", ".join(found) if found else "(none)"
        print(f"{label:<25} | {match_str:<20} | {found_str}")
    except Exception as e:
        print(f"{label:<25} | ERROR: {e}")

print()
print("Full outputs saved to: " + output_dir)
PYEOF

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  Precedence Test via Speakr API${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""
echo -e "${YELLOW}To test the full precedence chain (user → folder → tag → upload form),${NC}"
echo -e "${YELLOW}use the Speakr web API with authentication:${NC}"
echo ""
echo -e "1. Set user-level defaults in Account Settings → Prompt Options"
echo -e "2. Create a tag with different hotwords/initial_prompt"
echo -e "3. Create a folder with different hotwords/initial_prompt"
echo -e "4. Upload via API and check server logs for resolved values:"
echo ""
cat << 'EXAMPLE'
# Upload with user defaults only (no tag, no folder)
curl -X POST "https://your-speakr/upload" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@test.flac"
# → Should use user defaults

# Upload with a tag that has hotwords set
curl -X POST "https://your-speakr/upload" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@test.flac" \
  -F "tags=TAG_ID_WITH_HOTWORDS"
# → Should use tag defaults (overrides user)

# Upload with explicit form values (highest priority)
curl -X POST "https://your-speakr/upload" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@test.flac" \
  -F "tags=TAG_ID_WITH_HOTWORDS" \
  -F "hotwords=FormOverride1,FormOverride2" \
  -F "initial_prompt=Form level prompt"
# → Should use form values (overrides tag and user)
EXAMPLE

echo ""
echo -e "${GREEN}Test complete!${NC}"
