/**
 * Content-Disposition helpers.
 *
 * Downloads that go through fetch() + blob have to pull the filename out of the
 * header themselves. Servers send non-ASCII names as RFC 5987 `filename*`
 * (percent-encoded UTF-8), usually next to an ASCII-only `filename=` fallback,
 * so `filename*` has to be preferred or unicode titles silently degrade to the
 * fallback.
 */

/**
 * Extract the download filename from a Content-Disposition header.
 *
 * @param {string|null|undefined} header - raw Content-Disposition value
 * @param {string} fallback - name to use when the header carries none
 * @returns {string} the filename to hand to `a.download`
 */
export function filenameFromContentDisposition(header, fallback) {
    if (!header) return fallback;

    const utf8Match = /filename\*=\s*UTF-8''([^;]+)/i.exec(header);
    if (utf8Match) {
        try {
            const decoded = decodeURIComponent(utf8Match[1].trim());
            if (decoded) return decoded;
        } catch (e) {
            // Malformed percent-encoding: fall through to the ASCII parameter.
        }
    }

    const asciiMatch = /filename="([^"]+)"/.exec(header);
    if (asciiMatch && asciiMatch[1]) return asciiMatch[1];

    const bareMatch = /filename=([^;"\s]+)/.exec(header);
    if (bareMatch && bareMatch[1]) return bareMatch[1];

    return fallback;
}
