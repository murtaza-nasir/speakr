/**
 * /label/<name> deep links.
 *
 * A label link points at a group of recordings the way /recordings/<id> points
 * at a single one, so a tag can be hyperlinked and shared. The server only
 * serves the SPA shell for these paths; everything below resolves the name
 * against the tags the viewer can actually see, which is why two people
 * opening the same link can legitimately see different recordings.
 *
 * Tag names are not unique across that set: a user holds at most one tag per
 * name, but a group tag belongs to the admin who created it, so a member can
 * see both their own "foo" and the group's "foo". A label name therefore
 * resolves to a set of tag ids, and the link means their union.
 */

const normalize = (name) => String(name ?? '').trim().toLowerCase();

/**
 * Every tag named exactly `name`, ignoring case and surrounding whitespace.
 * Exact rather than substring: the link says "foo", so "foobar" is a
 * different label and must not ride along.
 */
export const matchTagsByLabelName = (name, availableTags = []) => {
    const wanted = normalize(name);
    if (!wanted) return [];
    return (availableTags || []).filter(tag => tag && normalize(tag.name) === wanted);
};

/**
 * The label name carried by a pathname, or null when it isn't a label link.
 */
export const parseLabelPath = (pathname) => {
    const match = String(pathname || '').match(/^\/label\/([^/]+)\/?$/);
    if (!match) return null;
    let name;
    try {
        name = decodeURIComponent(match[1]);
    } catch (_) {
        return null;  // malformed %-escape: not a link we can honour
    }
    return name.trim() || null;
};

/**
 * The path that represents a tag filter, or null when the filter can't be
 * expressed as one label link — nothing selected, or several distinct names,
 * which no single /label/<name> covers.
 *
 * A name containing '/' is also refused: werkzeug decodes %2F before routing,
 * so such a link would 404, and putting a dead URL in the address bar is
 * worse than leaving it alone.
 */
export const labelPathForFilter = (filterTagIds = [], availableTags = []) => {
    const names = new Set(
        (filterTagIds || [])
            .map(id => (availableTags || []).find(tag => tag && tag.id === id))
            .filter(Boolean)
            .map(tag => String(tag.name ?? '').trim())
            .filter(Boolean)
    );
    if (names.size !== 1) return null;
    const [name] = [...names];
    if (name.includes('/')) return null;
    return `/label/${encodeURIComponent(name)}`;
};
