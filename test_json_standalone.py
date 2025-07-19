#!/usr/bin/env python3
"""
Standalone test for JSON preprocessing functionality.
Tests the safe_json_loads function with various malformed JSON scenarios.
"""

import json
import re
import ast
from unittest.mock import Mock

# Mock logger for testing
class MockLogger:
    def warning(self, msg): print(f"WARNING: {msg}")
    def info(self, msg): print(f"INFO: {msg}")
    def debug(self, msg): print(f"DEBUG: {msg}")
    def error(self, msg): print(f"ERROR: {msg}")

# Create mock app with logger
class MockApp:
    logger = MockLogger()

app = MockApp()

def safe_json_loads(json_string, fallback_value=None):
    """
    Safely parse JSON with preprocessing to handle common LLM JSON formatting issues.
    
    Args:
        json_string (str): The JSON string to parse
        fallback_value: Value to return if parsing fails (default: None)
    
    Returns:
        Parsed JSON object or fallback_value if parsing fails
    """
    if not json_string or not isinstance(json_string, str):
        app.logger.warning(f"Invalid JSON input: {type(json_string)} - {json_string}")
        return fallback_value
    
    # Step 1: Clean the input string
    cleaned_json = json_string.strip()
    
    # Step 2: Extract JSON from markdown code blocks if present
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', cleaned_json, re.DOTALL)
    if json_match:
        cleaned_json = json_match.group(1).strip()
    
    # Step 3: Try multiple parsing strategies
    parsing_strategies = [
        # Strategy 1: Direct parsing (for well-formed JSON)
        lambda x: json.loads(x),
        
        # Strategy 2: Fix common escape issues
        lambda x: json.loads(preprocess_json_escapes(x)),
        
        # Strategy 3: Use ast.literal_eval as fallback for simple cases
        lambda x: ast.literal_eval(x) if x.startswith(('{', '[')) else None,
        
        # Strategy 4: Extract JSON object/array using regex
        lambda x: json.loads(extract_json_object(x)),
    ]
    
    for i, strategy in enumerate(parsing_strategies):
        try:
            result = strategy(cleaned_json)
            if result is not None:
                if i > 0:  # Log if we had to use a fallback strategy
                    app.logger.info(f"JSON parsed successfully using strategy {i+1}")
                return result
        except (json.JSONDecodeError, ValueError, SyntaxError) as e:
            if i == 0:  # Only log the first failure to avoid spam
                app.logger.debug(f"JSON parsing strategy {i+1} failed: {e}")
            continue
    
    # All strategies failed
    app.logger.error(f"All JSON parsing strategies failed for: {cleaned_json[:200]}...")
    return fallback_value

def preprocess_json_escapes(json_string):
    """
    Preprocess JSON string to fix common escape issues from LLM responses.
    Uses a more sophisticated approach to handle nested quotes properly.
    """
    if not json_string:
        return json_string
    
    result = []
    i = 0
    in_string = False
    escape_next = False
    expecting_value = False  # Track if we're expecting a value (after :)
    
    while i < len(json_string):
        char = json_string[i]
        
        if escape_next:
            # This character is escaped, add it as-is
            result.append(char)
            escape_next = False
        elif char == '\\':
            # This is an escape character
            result.append(char)
            escape_next = True
        elif char == ':' and not in_string:
            # We found a colon, next string will be a value
            result.append(char)
            expecting_value = True
        elif char == ',' and not in_string:
            # We found a comma, reset expecting_value
            result.append(char)
            expecting_value = False
        elif char == '"':
            if not in_string:
                # Starting a string
                in_string = True
                result.append(char)
            else:
                # We're in a string, check if this quote should be escaped
                # Look ahead to see if this is the end of the string value
                j = i + 1
                while j < len(json_string) and json_string[j].isspace():
                    j += 1
                
                # For keys (not expecting_value), only end on colon
                # For values (expecting_value), end on comma, closing brace, or closing bracket
                if expecting_value:
                    end_chars = ',}]'
                else:
                    end_chars = ':'
                
                if j < len(json_string) and json_string[j] in end_chars:
                    # This is the end of the string
                    in_string = False
                    result.append(char)
                    if not expecting_value:
                        # We just finished a key, next will be expecting value
                        expecting_value = True
                else:
                    # This is an inner quote that should be escaped
                    result.append('\\"')
        else:
            result.append(char)
        
        i += 1
    
    return ''.join(result)

def extract_json_object(text):
    """
    Extract the first complete JSON object or array from text using regex.
    """
    # Look for JSON object
    obj_match = re.search(r'\{.*\}', text, re.DOTALL)
    if obj_match:
        return obj_match.group(0)
    
    # Look for JSON array
    arr_match = re.search(r'\[.*\]', text, re.DOTALL)
    if arr_match:
        return arr_match.group(0)
    
    # Return original if no JSON structure found
    return text

def run_comprehensive_test():
    """Run a comprehensive test with various malformed JSON examples."""
    print("🧪 Running comprehensive JSON preprocessing tests...\n")
    
    test_cases = [
        {
            "name": "Valid JSON",
            "input": '{"title": "Test", "summary": "Valid JSON"}',
            "should_succeed": True
        },
        {
            "name": "Unescaped quotes in title",
            "input": '{"title": "Meeting about "Project X"", "summary": "Discussion summary"}',
            "should_succeed": True
        },
        {
            "name": "Multiple unescaped quotes",
            "input": '{"title": "John said "Hello" and Mary replied "Hi there"", "summary": "Conversation log"}',
            "should_succeed": True
        },
        {
            "name": "Markdown code block",
            "input": '```json\n{"title": "Wrapped", "summary": "In code block"}\n```',
            "should_succeed": True
        },
        {
            "name": "Mixed quotes and apostrophes",
            "input": '{"title": "Alice\'s "big idea" presentation", "summary": "She said it\'s "revolutionary""}',
            "should_succeed": True
        },
        {
            "name": "JSON with newlines",
            "input": '{"title": "Multi-line", "summary": "Line 1\\nLine 2 with \\"quotes\\"\\nLine 3"}',
            "should_succeed": True
        },
        {
            "name": "LLM response with explanation",
            "input": '''Here's the JSON:
```json
{
  "title": "Q3 Planning",
  "summary": "We discussed the "new initiative" for next quarter."
}
```
Hope this helps!''',
            "should_succeed": True
        },
        {
            "name": "Speaker identification with quotes",
            "input": '{"SPEAKER_00": "John Smith", "SPEAKER_01": "Jane "The Expert" Doe", "SPEAKER_02": "Bob"}',
            "should_succeed": True
        },
        {
            "name": "Completely malformed",
            "input": '{"title": "Test", "summary": this is not valid json at all}',
            "should_succeed": False
        },
        {
            "name": "Empty string",
            "input": "",
            "should_succeed": False
        }
    ]
    
    passed = 0
    failed = 0
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"Test {i}: {test_case['name']}")
        print(f"Input: {test_case['input'][:100]}{'...' if len(test_case['input']) > 100 else ''}")
        
        try:
            result = safe_json_loads(test_case['input'], {"error": "fallback"})
            
            if test_case['should_succeed']:
                if result != {"error": "fallback"} and isinstance(result, (dict, list)):
                    print("✅ PASSED - Successfully parsed JSON")
                    print(f"   Result: {result}")
                    passed += 1
                else:
                    print("❌ FAILED - Expected successful parsing but got fallback")
                    failed += 1
            else:
                if result == {"error": "fallback"}:
                    print("✅ PASSED - Correctly returned fallback for malformed JSON")
                    passed += 1
                else:
                    print("❌ FAILED - Expected fallback but got parsed result")
                    print(f"   Unexpected result: {result}")
                    failed += 1
                    
        except Exception as e:
            print(f"❌ FAILED - Exception occurred: {e}")
            failed += 1
        
        print("-" * 50)
    
    print(f"\n📊 Test Results: {passed} passed, {failed} failed")
    return failed == 0

def test_preprocessing_function():
    """Test the preprocessing function directly."""
    print("\n🔧 Testing preprocessing function directly...\n")
    
    test_input = '{"title": "Meeting about "Project X"", "summary": "Discussion summary"}'
    print(f"Original: {test_input}")
    
    processed = preprocess_json_escapes(test_input)
    print(f"Processed: {processed}")
    
    try:
        result = json.loads(processed)
        print(f"✅ Successfully parsed: {result}")
    except json.JSONDecodeError as e:
        print(f"❌ Still failed: {e}")

def test_specific_scenarios():
    """Test specific real-world scenarios."""
    print("\n🎯 Testing specific LLM response scenarios...\n")
    
    # Test case from the original issue
    gemini_response = '''{"title": "Meeting about "Project Phoenix" and budget allocation", "summary": "The team discussed John's "breakthrough idea" and Mary said "this will change everything" during the Q3 planning session."}'''
    
    print("Testing Gemini-style response with unescaped quotes:")
    print(f"Input: {gemini_response}")
    
    # Test preprocessing directly
    processed = preprocess_json_escapes(gemini_response)
    print(f"Processed: {processed}")
    
    result = safe_json_loads(gemini_response)
    if isinstance(result, dict) and "title" in result and "summary" in result:
        print("✅ SUCCESS - Parsed Gemini response correctly!")
        print(f"Title: {result['title']}")
        print(f"Summary: {result['summary'][:100]}...")
    else:
        print("❌ FAILED - Could not parse Gemini response")
        print(f"Result: {result}")
    
    print("-" * 50)
    
    # Test speaker identification scenario
    speaker_response = '''{"SPEAKER_00": "John "The Manager" Smith", "SPEAKER_01": "Alice Johnson", "SPEAKER_02": "Bob "Tech Lead" Wilson"}'''
    
    print("Testing speaker identification with quotes in names:")
    print(f"Input: {speaker_response}")
    
    # Test preprocessing directly
    processed = preprocess_json_escapes(speaker_response)
    print(f"Processed: {processed}")
    
    result = safe_json_loads(speaker_response)
    if isinstance(result, dict) and len(result) >= 3:
        print("✅ SUCCESS - Parsed speaker identification correctly!")
        for speaker, name in result.items():
            print(f"  {speaker}: {name}")
    else:
        print("❌ FAILED - Could not parse speaker identification")
        print(f"Result: {result}")

if __name__ == "__main__":
    print("🚀 Starting Standalone JSON Preprocessing Tests\n")
    
    # Test preprocessing function directly
    test_preprocessing_function()
    
    # Run the comprehensive test
    success = run_comprehensive_test()
    
    # Test specific scenarios
    test_specific_scenarios()
    
    if success:
        print("\n🎉 All tests completed successfully! JSON preprocessing should handle LLM response issues gracefully.")
    else:
        print("\n⚠️  Some tests failed. The implementation may need refinement.")
