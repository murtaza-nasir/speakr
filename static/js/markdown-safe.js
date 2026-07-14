/**
 * Safe markdown rendering.
 *
 * marked.parse() passes raw inline HTML through, and its output is assigned to
 * v-html sinks (chat/Inquire answers, admin banner/disclaimers). That content
 * originates from LLM output and admin settings, so an embedded
 * `<img src=x onerror=...>` would execute. Every such path must go through
 * this helper, which sanitizes the parsed HTML with DOMPurify.
 *
 * Fails closed: if DOMPurify is somehow unavailable, the text is emitted
 * escaped rather than as raw HTML.
 */
(function () {
  function renderMarkdownSafe(text) {
    if (text === null || text === undefined) return '';
    var html;
    try {
      html = (window.marked && window.marked.parse)
        ? window.marked.parse(text)
        : String(text);
    } catch (e) {
      html = String(text);
    }
    if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
      return window.DOMPurify.sanitize(html);
    }
    var div = document.createElement('div');
    div.textContent = html;
    return div.innerHTML;
  }
  window.renderMarkdownSafe = renderMarkdownSafe;
})();
