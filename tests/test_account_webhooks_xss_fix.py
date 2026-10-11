#!/usr/bin/env python3
"""Regression test for XSS fix in static/js/account-webhooks.js.

Covers the safeInsert / DOMPurify guard that sanitizes dynamic HTML
before insertAdjacentHTML in the webhook deliveries panel.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_account_webhooks_xss_fix_exists():
    """Verify the fix file exists and contains the safeInsert wrapper."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "speakr.git", "static", "js", "account-webhooks.js")
    assert os.path.isfile(path), f"Fix file missing: {path}"
    content = open(path).read()
    assert "function safeInsert" in content, "safeInsert helper missing"
    assert "window.DOMPurify.sanitize" in content, "DOMPurify sanitize missing"
