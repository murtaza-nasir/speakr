"""
JSON parsing utilities for handling LLM responses and malformed JSON.

This module provides robust JSON parsing functions that can handle common
issues with LLM-generated JSON, including:
- Incomplete/unterminated JSON structures
- Escape sequence problems
- JSON embedded in markdown code blocks
- Nested quotes and special characters
"""

import json
import re
import ast
import logging

# Module-level logger
logger = logging.getLogger(__name__)


def auto_close_json(json_string):
    """
    Attempts to close an incomplete JSON string by appending necessary brackets and braces.
    This is a simplified parser and may not handle all edge cases, but is
    designed to fix unterminated strings from API responses.
    """
    if not isinstance(json_string, str):
        return json_string

    stack = []
    in_string = False
    escape_next = False

    for char in json_string:
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        if char == '"':
            # We don't handle escaped quotes inside strings perfectly,
            # but this is a simple heuristic.
            if not escape_next:
                in_string = not in_string

        if not in_string:
            if char == '{':
                stack.append('}')
            elif char == '[':
                stack.append(']')
            elif char == '}':
                if stack and stack[-1] == '}':
                    stack.pop()
            elif char == ']':
                if stack and stack[-1] == ']':
                    stack.pop()

    # If we are inside a string at the end, close it.
    if in_string:
        json_string += '"'

    # Close any remaining open structures
    while stack:
        json_string += stack.pop()

    return json_string


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
        logger.warning(f"Invalid JSON input: {type(json_string)} - {json_string}")
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

        # Strategy 5: Auto-close incomplete JSON and parse
        lambda x: json.loads(auto_close_json(x)),
    ]

    for i, strategy in enumerate(parsing_strategies):
        try:
            result = strategy(cleaned_json)
            if result is not None:
                if i > 0:  # Log if we had to use a fallback strategy
                    logger.info(f"JSON parsed successfully using strategy {i+1}")
                return result
        except (json.JSONDecodeError, ValueError, SyntaxError) as e:
            if i == 0:  # Only log the first failure to avoid spam
                logger.debug(f"JSON parsing strategy {i+1} failed: {e}")
            continue

    # All strategies failed
    logger.error(f"All JSON parsing strategies failed for: {cleaned_json[:200]}...")
    return fallback_value
