import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// asr-languages.js is a classic script that sets window.SpeakrASRLanguages.
const SOURCE = readFileSync(fileURLToPath(new URL('./asr-languages.js', import.meta.url)), 'utf8');
const SERVER = readFileSync(fileURLToPath(new URL('../../src/utils/language.py', import.meta.url)), 'utf8');

function load(i18n) {
    const window = { i18n };
    vm.runInContext(SOURCE, vm.createContext({ window, Intl }));
    return window.SpeakrASRLanguages;
}

// (name, code) pairs of OUTPUT_LANGUAGES in src/utils/language.py.
function serverOutputLanguages() {
    const block = SERVER.match(/OUTPUT_LANGUAGES = \[([\s\S]*?)\n\]/)[1];
    return [...block.matchAll(/\("([^"]+)", "([^"]+)"\)/g)].map(m => [m[1], m[2]]);
}

function fakeI18n(locale, languages) {
    return {
        currentLocale: locale,
        getNestedTranslation(loc, key) {
            const code = key.replace('languages.', '');
            return (loc === locale && languages[code]) || null;
        },
    };
}

describe('asr-languages', () => {
    it('builds 100 options sorted by label', () => {
        const options = load().buildOptions('en');
        expect(options).toHaveLength(100);
        const labels = options.map(o => o.label);
        expect(labels).toEqual([...labels].sort((a, b) => a.localeCompare(b, 'en')));
        expect(options.find(o => o.value === 'cs').label).toBe('Czech');
        expect(options.some(o => o.value === 'pt-BR')).toBe(false);
    });

    it('labels the options in the interface language', () => {
        expect(load().buildOptions('de').find(o => o.value === 'cs').label).toBe('Tschechisch');
    });

    it('the server output languages are the shared list under its English names', () => {
        const server = new Map(serverOutputLanguages().map(([name, code]) => [code, name]));
        const options = load().buildOptions('en');
        expect(server.size).toBe(options.length + 1);
        expect(server.get('pt-BR')).toBe('Portuguese (Brazilian)');
        for (const { value, label } of options) {
            // The stored name is plain ASCII: "Maori" for "Māori".
            expect(server.get(value)).toBe(label.normalize('NFD').replace(/[̀-ͯ]/g, ''));
        }
    });

    it('outputLabel prefers the locale key, then the name of the language, then the English name', () => {
        const shared = load(fakeI18n('de', { de: 'Deutsch', 'pt-BR': 'Portugiesisch (Brasilien)' }));
        expect(shared.outputLabel('German', 'de')).toBe('Deutsch');
        expect(shared.outputLabel('Portuguese (Brazilian)', 'pt-BR')).toBe('Portugiesisch (Brasilien)');
        expect(shared.outputLabel('Czech', 'cs')).toBe('Tschechisch');
        expect(shared.outputLabel('Hawaiian', 'haw', 'en')).toBe('Hawaiian');
        expect(shared.outputLabel('Klingon', '')).toBe('Klingon');
    });

    it('setValue keeps a value outside the list as its own option', () => {
        const options = [{ value: '' }, { value: 'en' }];
        const select = {
            options,
            value: '',
            appendChild(opt) { options.push(opt); },
        };
        const shared = vm.runInContext(
            '(function(){ return window.SpeakrASRLanguages; })()',
            (() => {
                const window = {};
                const context = vm.createContext({
                    window, Intl,
                    document: { createElement: () => ({ value: '', textContent: '' }) },
                });
                vm.runInContext(SOURCE, context);
                return context;
            })());
        shared.setValue(select, 'xx-legacy');
        expect(options.map(o => o.value)).toEqual(['', 'en', 'xx-legacy']);
        expect(select.value).toBe('xx-legacy');
        shared.setValue(select, 'en');
        expect(options).toHaveLength(3);
        expect(select.value).toBe('en');
    });
});
