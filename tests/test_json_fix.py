import json
import sys
import os

# Add the parent directory to the path to import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auto_close_json, safe_json_loads

def run_tests():
    """Runs a series of tests for the JSON fixing functions."""
    
    test_cases_auto_close = {
        "Unterminated string": ('{"title": "Test", "summary": "This is a test', '{"title": "Test", "summary": "This is a test"}'),
        "Missing closing brace": ('{"title": "Test", "summary": "This is a test"}', '{"title": "Test", "summary": "This is a test"}'),
        "Missing closing bracket": ('[{"item": 1}, {"item": 2}', '[{"item": 1}, {"item": 2}]'),
        "Nested unterminated": ('{"data": {"items": [1, 2', '{"data": {"items": [1, 2]}}'),
        "String at the end": ('{"key": "value', '{"key": "value"}'),
        "Empty string": ('', ''),
        "Already valid": ('{"a": 1}', '{"a": 1}'),
        "Complex nested object": ('{"a": {"b": {"c": [1, 2, {"d": "e' , '{"a": {"b": {"c": [1, 2, {"d": "e"}]}}}')
    }

    print("--- Testing auto_close_json ---")
    for name, (input_str, expected_str) in test_cases_auto_close.items():
        result = auto_close_json(input_str)
        print(f"Test: {name}")
        print(f"  Input:    '{input_str}'")
        print(f"  Output:   '{result}'")
        print(f"  Expected: '{expected_str}'")
        assert result == expected_str, f"Failed: {name}"
        print("  Result: PASSED\n")

    test_cases_safe_loads = {
        "Unterminated string": '{"title": "Test", "summary": "This is a test',
        "Markdown with unterminated JSON": '```json\n{"title": "Test", "summary": "This is a test\n```',
        "Missing closing brace": '{"title": "Test", "summary": "This is a test"}',
        "Valid JSON": '{"title": "Complete", "summary": "This is a complete JSON."}',
        "JSON with escaped quotes": '{"title": "Escaped", "summary": "This is a \\"test\\" with quotes."}',
        "Invalid JSON": 'this is not json',
    }

    print("\n--- Testing safe_json_loads ---")
    for name, input_str in test_cases_safe_loads.items():
        result = safe_json_loads(input_str)
        print(f"Test: {name}")
        print(f"  Input: '{input_str}'")
        print(f"  Output: {result}")
        if name == "Invalid JSON":
            assert result is None, f"Failed: {name}"
            print("  Result: PASSED (Correctly returned None)\n")
        else:
            assert isinstance(result, dict), f"Failed: {name}"
            print("  Result: PASSED\n")

if __name__ == "__main__":
    run_tests()
    print("All tests completed successfully!")
