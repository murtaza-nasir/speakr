#!/usr/bin/env python3
"""
Test suite for JSON preprocessing functionality in Speakr app.
Tests the safe_json_loads function with various malformed JSON scenarios.
"""

import sys
import os
import json
import unittest
from unittest.mock import Mock

# Add the app directory to the path so we can import from app.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock the Flask app and logger for testing
class MockApp:
    def __init__(self):
        self.logger = Mock()

# Set up the mock app before importing
app = MockApp()

# Import the functions we want to test
from src.app import safe_json_loads, preprocess_json_escapes, extract_json_object

class TestJSONPreprocessing(unittest.TestCase):
    """Test cases for JSON preprocessing functionality."""
    
    def test_valid_json(self):
        """Test that valid JSON is parsed correctly."""
        valid_json = '{"title": "Test Meeting", "summary": "This is a test summary"}'
        result = safe_json_loads(valid_json)
        expected = {"title": "Test Meeting", "summary": "This is a test summary"}
        self.assertEqual(result, expected)
    
    def test_json_with_markdown_code_blocks(self):
        """Test JSON wrapped in markdown code blocks."""
        markdown_json = '''```json
{
  "title": "Meeting Notes",
  "summary": "Key points discussed"
}
```'''
        result = safe_json_loads(markdown_json)
        expected = {"title": "Meeting Notes", "summary": "Key points discussed"}
        self.assertEqual(result, expected)
    
    def test_json_with_unescaped_quotes(self):
        """Test JSON with unescaped quotes in string values."""
        malformed_json = '{"title": "John said "Hello world" to everyone", "summary": "Meeting summary"}'
        result = safe_json_loads(malformed_json)
        expected = {"title": 'John said "Hello world" to everyone', "summary": "Meeting summary"}
        self.assertEqual(result, expected)
    
    def test_json_with_mixed_quotes(self):
        """Test JSON with mixed quote scenarios."""
        malformed_json = '{"title": "Alice\'s "big idea" presentation", "summary": "She said "this will change everything""}'
        result = safe_json_loads(malformed_json)
        self.assertIsInstance(result, dict)
        self.assertIn("title", result)
        self.assertIn("summary", result)
    
    def test_json_with_newlines_and_special_chars(self):
        """Test JSON with newlines and special characters."""
        malformed_json = '''{"title": "Complex Meeting", "summary": "Discussion about:\n- Point 1\n- Point 2 with "quotes"\n- Point 3"}'''
        result = safe_json_loads(malformed_json)
        self.assertIsInstance(result, dict)
        self.assertIn("title", result)
        self.assertIn("summary", result)
    
    def test_empty_or_invalid_input(self):
        """Test handling of empty or invalid input."""
        # Empty string
        result = safe_json_loads("", {"default": "value"})
        self.assertEqual(result, {"default": "value"})
        
        # None input
        result = safe_json_loads(None, {"default": "value"})
        self.assertEqual(result, {"default": "value"})
        
        # Non-string input
        result = safe_json_loads(123, {"default": "value"})
        self.assertEqual(result, {"default": "value"})
    
    def test_completely_malformed_json(self):
        """Test completely malformed JSON that can't be fixed."""
        malformed_json = '{"title": "Test", "summary": unclosed string and missing quotes}'
        result = safe_json_loads(malformed_json, {"error": "fallback"})
        self.assertEqual(result, {"error": "fallback"})
    
    def test_json_with_nested_quotes(self):
        """Test JSON with deeply nested quote scenarios."""
        malformed_json = '{"title": "Meeting about "Project Alpha" and "Project Beta"", "summary": "Both projects involve "cutting-edge" technology"}'
        result = safe_json_loads(malformed_json)
        self.assertIsInstance(result, dict)
        # Should have successfully parsed something
        self.assertTrue(len(result) > 0)
    
    def test_json_array_format(self):
        """Test JSON array format (for transcription data)."""
        json_array = '[{"speaker": "John", "sentence": "Hello everyone"}, {"speaker": "Jane", "sentence": "Good morning"}]'
        result = safe_json_loads(json_array)
        expected = [{"speaker": "John", "sentence": "Hello everyone"}, {"speaker": "Jane", "sentence": "Good morning"}]
        self.assertEqual(result, expected)
    
    def test_preprocess_json_escapes_function(self):
        """Test the preprocess_json_escapes function directly."""
        input_json = '{"title": "John said "Hello" to Mary", "summary": "Simple test"}'
        processed = preprocess_json_escapes(input_json)
        # Should be valid JSON after preprocessing
        result = json.loads(processed)
        self.assertIsInstance(result, dict)
        self.assertIn("title", result)
        self.assertIn("summary", result)
    
    def test_extract_json_object_function(self):
        """Test the extract_json_object function directly."""
        # Test with extra text around JSON object
        text_with_json = 'Here is some text {"title": "Test", "summary": "Content"} and more text'
        extracted = extract_json_object(text_with_json)
        result = json.loads(extracted)
        expected = {"title": "Test", "summary": "Content"}
        self.assertEqual(result, expected)
        
        # Test with JSON array
        text_with_array = 'Some text [{"item": "one"}, {"item": "two"}] more text'
        extracted = extract_json_object(text_with_array)
        result = json.loads(extracted)
        expected = [{"item": "one"}, {"item": "two"}]
        self.assertEqual(result, expected)
    
    def test_real_world_llm_response_scenarios(self):
        """Test real-world scenarios that might come from LLM responses."""
        
        # Scenario 1: LLM response with explanation text
        llm_response1 = '''Here's the JSON response you requested:

```json
{
  "title": "Q3 Planning Meeting",
  "summary": "We discussed the "new initiative" and John's "breakthrough idea" for next quarter."
}
```

This should help with your transcription needs.'''
        
        result1 = safe_json_loads(llm_response1)
        self.assertIsInstance(result1, dict)
        self.assertIn("title", result1)
        self.assertIn("summary", result1)
        
        # Scenario 2: LLM response with unescaped quotes and no code blocks
        llm_response2 = '{"title": "Team Standup", "summary": "Alice mentioned "the deadline is tight" and Bob said "we need more resources""}'
        
        result2 = safe_json_loads(llm_response2)
        self.assertIsInstance(result2, dict)
        self.assertIn("title", result2)
        self.assertIn("summary", result2)
        
        # Scenario 3: LLM response with speaker identification
        llm_response3 = '''{"SPEAKER_00": "John Smith", "SPEAKER_01": "Jane "The Expert" Doe", "SPEAKER_02": "Bob"}'''
        
        result3 = safe_json_loads(llm_response3)
        self.assertIsInstance(result3, dict)
        self.assertTrue(len(result3) >= 2)  # Should have parsed at least some speakers
    
    def test_fallback_strategies(self):
        """Test that different parsing strategies work as fallbacks."""
        
        # Test ast.literal_eval fallback for simple cases
        simple_dict = "{'title': 'Simple', 'summary': 'Test'}"
        result = safe_json_loads(simple_dict)
        expected = {"title": "Simple", "summary": "Test"}
        self.assertEqual(result, expected)
        
        # Test regex extraction fallback
        messy_response = 'Some text before {"title": "Extracted", "summary": "From regex"} some text after'
        result = safe_json_loads(messy_response)
        expected = {"title": "Extracted", "summary": "From regex"}
        self.assertEqual(result, expected)
    
    def test_performance_with_large_content(self):
        """Test performance with larger JSON content."""
        large_summary = "This is a very long summary. " * 100  # Create a long string
        large_json = f'{{"title": "Large Content Test", "summary": "{large_summary}"}}'
        
        result = safe_json_loads(large_json)
        self.assertIsInstance(result, dict)
        self.assertIn("title", result)
        self.assertIn("summary", result)
        self.assertEqual(result["title"], "Large Content Test")

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
                if result != {"error": "fallback"} and isinstance(result, dict):
                    print("✅ PASSED - Successfully parsed JSON")
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
                    failed += 1
                    
        except Exception as e:
            print(f"❌ FAILED - Exception occurred: {e}")
            failed += 1
        
        print("-" * 50)
    
    print(f"\n📊 Test Results: {passed} passed, {failed} failed")
    return failed == 0

if __name__ == "__main__":
    print("🚀 Starting JSON Preprocessing Tests for Speakr App\n")
    
    # Run the comprehensive manual test
    manual_success = run_comprehensive_test()
    
    print("\n" + "="*60)
    print("🔬 Running Unit Tests")
    print("="*60)
    
    # Run the unit tests
    unittest.main(argv=[''], exit=False, verbosity=2)
    
    if manual_success:
        print("\n🎉 All tests completed! JSON preprocessing should handle LLM response issues gracefully.")
    else:
        print("\n⚠️  Some tests failed. Please review the implementation.")
