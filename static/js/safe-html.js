/**
 * safeHtml: HTML built from a template literal, with every value escaped.
 *
 *   el.innerHTML = safeHtml`<a href="/recordings/${id}" title="${title}">${title}</a>`;
 *
 * Each interpolated value is escaped for text and for double- or
 * single-quoted attributes (& < > " '). The only values inserted as markup
 * are the results of safeHtml itself, so a card can be put together from
 * smaller pieces; an array is inserted item by item, with no separator.
 * null, undefined and false insert nothing.
 *
 * Every innerHTML, outerHTML and insertAdjacentHTML assignment of the pages
 * takes a safeHtml result or a constant string, never a plain template
 * literal or a concatenation (#692; tests/test_innerhtml_template_sinks.py).
 * Escaping does not make a URL safe: an interpolated href or src must be a
 * path the page builds itself, not a value it received. A value never goes
 * into an inline event handler; the page adds a listener instead.
 */
(function () {
  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (c) { return ESCAPES[c]; });
  }

  function SafeHtml(html) {
    this.html = html;
  }
  SafeHtml.prototype.toString = function () { return this.html; };

  function render(value) {
    if (value === null || value === undefined || value === false) return '';
    if (value instanceof SafeHtml) return value.html;
    if (Array.isArray(value)) return value.map(render).join('');
    return escapeHtml(value);
  }

  function safeHtml(strings) {
    var out = strings[0];
    for (var i = 1; i < strings.length; i++) {
      out += render(arguments[i]) + strings[i];
    }
    return new SafeHtml(out);
  }

  safeHtml.escape = escapeHtml;
  safeHtml.isSafe = function (value) { return value instanceof SafeHtml; };
  window.safeHtml = safeHtml;
})();
