import { describe, expect, it } from 'vitest';

import {
    labelPathForFilter,
    matchTagsByLabelName,
    parseLabelPath
} from './label-links.js';

const tag = (id, name) => ({ id, name });

describe('matchTagsByLabelName', () => {
    const tags = [
        tag(1, 'foo'),
        tag(2, 'foobar'),
        tag(3, 'Team Sync'),
        tag(4, 'FOO')          // e.g. a group tag alongside the personal one
    ];

    it('matches exactly, so foo never drags in foobar', () => {
        expect(matchTagsByLabelName('foo', tags).map(t => t.id)).toEqual([1, 4]);
    });

    it('ignores case and surrounding whitespace on both sides', () => {
        expect(matchTagsByLabelName('  TEAM sync ', [tag(9, ' Team Sync ')]))
            .toEqual([tag(9, ' Team Sync ')]);
    });

    it('returns every tag sharing the name so the link means their union', () => {
        expect(matchTagsByLabelName('FoO', tags)).toHaveLength(2);
    });

    it('returns nothing for an unknown, blank, or missing name', () => {
        expect(matchTagsByLabelName('nope', tags)).toEqual([]);
        expect(matchTagsByLabelName('   ', tags)).toEqual([]);
        expect(matchTagsByLabelName(null, tags)).toEqual([]);
        expect(matchTagsByLabelName(undefined, undefined)).toEqual([]);
    });

    it('survives malformed tag entries', () => {
        expect(matchTagsByLabelName('foo', [null, {}, tag(1, 'foo')]).map(t => t.id))
            .toEqual([1]);
    });
});

describe('parseLabelPath', () => {
    it('reads the name out of a label path', () => {
        expect(parseLabelPath('/label/foo')).toBe('foo');
        expect(parseLabelPath('/label/foo/')).toBe('foo');
    });

    it('decodes percent-escapes, so spaced names round-trip', () => {
        expect(parseLabelPath('/label/Team%20Sync')).toBe('Team Sync');
        expect(parseLabelPath('/label/caf%C3%A9')).toBe('café');
    });

    it('leaves + alone: it is a literal plus in a path segment, not a space', () => {
        expect(parseLabelPath('/label/a+b')).toBe('a+b');
    });

    it('is not fooled by other routes', () => {
        expect(parseLabelPath('/')).toBeNull();
        expect(parseLabelPath('/recordings/12')).toBeNull();
        expect(parseLabelPath('/label')).toBeNull();
        expect(parseLabelPath('/label/')).toBeNull();
        expect(parseLabelPath('/label/a/b')).toBeNull();
        expect(parseLabelPath('/labels/foo')).toBeNull();
    });

    it('treats a malformed escape as no deep link rather than throwing', () => {
        expect(parseLabelPath('/label/%E0%A4%A')).toBeNull();
    });
});

describe('labelPathForFilter', () => {
    const tags = [tag(1, 'foo'), tag(2, 'FOO'), tag(3, 'Team Sync'), tag(4, 'bar')];

    it('links a single selected tag', () => {
        expect(labelPathForFilter([1], tags)).toBe('/label/foo');
    });

    it('percent-encodes so the path stays a single segment', () => {
        expect(labelPathForFilter([3], tags)).toBe('/label/Team%20Sync');
    });

    it('links several tags that share one name, since the link is their union', () => {
        // Distinct ids, one name — exactly what /label/foo resolves back to.
        expect(labelPathForFilter([1, 2], [tag(1, 'foo'), tag(2, 'foo')]))
            .toBe('/label/foo');
    });

    it('refuses two distinct names: no single label link covers them', () => {
        expect(labelPathForFilter([1, 4], tags)).toBeNull();
    });

    it('refuses an empty or unresolvable filter', () => {
        expect(labelPathForFilter([], tags)).toBeNull();
        expect(labelPathForFilter([99], tags)).toBeNull();
        expect(labelPathForFilter(undefined, undefined)).toBeNull();
    });

    it('refuses a name with a slash, which would 404 rather than route', () => {
        expect(labelPathForFilter([5], [tag(5, 'q1/q2')])).toBeNull();
    });

    it('round-trips with parseLabelPath for names needing encoding', () => {
        for (const name of ['foo', 'Team Sync', 'café', 'a+b', '100%']) {
            const path = labelPathForFilter([7], [tag(7, name)]);
            expect(parseLabelPath(path)).toBe(name);
        }
    });
});
