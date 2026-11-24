/**
 * Safe HTML for v-html sinks.
 *
 * marked.parse() passes raw inline HTML through, and its output is assigned to
 * v-html sinks (chat/Inquire answers, admin banner/disclaimers). That content
 * originates from LLM output and admin settings, so an embedded
 * `<img src=x onerror=...>` would execute. Every such path must go through
 * renderMarkdownSafe(), which sanitizes the parsed HTML with DOMPurify.
 *
 * HTML that arrives ready-made, such as the server's summary_html and
 * notes_html, goes through sanitizeHtml() at the sink as well (#695): the
 * server cleans it too, but the page cannot tell that the bytes it received
 * came from that code path.
 *
 * Both fail closed: if DOMPurify is somehow unavailable, the text is emitted
 * escaped rather than as raw HTML.
 */
(function () {
  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeText(value) {
    return String(value).replace(/[&<>"']/g, function (c) { return ESCAPES[c]; });
  }

  function sanitizeHtml(html) {
    if (html === null || html === undefined || html === '') return '';
    if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
      return window.DOMPurify.sanitize(String(html));
    }
    return escapeText(html);
  }

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
    return sanitizeHtml(html);
  }

  window.sanitizeHtml = sanitizeHtml;
  window.renderMarkdownSafe = renderMarkdownSafe;
})();
