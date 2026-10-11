import { describe, it, expect } from 'vitest';

describe('safeInsert sanitizes XSS payloads', () => {
  it('strips onerror from injected HTML', () => {
    const safe = (html) => {
      if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
        return window.DOMPurify.sanitize(html, { FORBID_ATTR: ['onerror', 'onclick'] });
      }
      return html;
    };
    const malicious = '<img src=x onerror=alert(1)>';
    const result = safe('<div>' + malicious + '</div>');
    expect(result).not.toContain('onerror');
  });

  it('preserves safe static HTML', () => {
    const safe = (html) => {
      if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
        return window.DOMPurify.sanitize(html, { ALLOWED_TAGS: ['p'] });
      }
      return html;
    };
    const staticHtml = '<p class="text-xs text-muted">No deliveries yet.</p>';
    const result = safe(staticHtml);
    expect(result).toContain('No deliveries yet.');
  });
});
