/**
 * Find and replace in transcript text (#440).
 *
 * A search is literal text, never a regular expression. "Whole words" means
 * the match is not part of a longer word in any script (letters, digits and
 * underscore count as word characters).
 */

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** A global RegExp for the search, or null when there is nothing to find. */
export function buildMatcher(find, { matchCase = false, wholeWord = false } = {}) {
    if (!find) return null;
    let source = escapeRegExp(find);
    if (wholeWord) source = `(?<![\\p{L}\\p{N}_])${source}(?![\\p{L}\\p{N}_])`;
    return new RegExp(source, `gu${matchCase ? '' : 'i'}`);
}

/** Every match in a list of texts, as { index, start, end }, in reading order. */
export function findMatches(texts, matcher) {
    const matches = [];
    if (!matcher) return matches;
    texts.forEach((text, index) => {
        if (!text) return;
        matcher.lastIndex = 0;
        for (const m of String(text).matchAll(matcher)) {
            matches.push({ index, start: m.index, end: m.index + m[0].length });
        }
    });
    return matches;
}

/** The text with every match replaced, and the number of replacements. */
export function replaceAllIn(text, matcher, replacement) {
    if (!matcher || !text) return { text, count: 0 };
    let count = 0;
    const out = String(text).replace(matcher, () => {
        count += 1;
        return replacement;
    });
    return { text: out, count };
}

/** The text with the match at [start, end) replaced. */
export function replaceAt(text, start, end, replacement) {
    return text.slice(0, start) + replacement + text.slice(end);
}
