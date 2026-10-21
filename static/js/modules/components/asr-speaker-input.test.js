import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {AsrSpeakerInput} from './asr-speaker-input.js';

let props, picker, emit, disabledChanged;
const key = (name, extra = {}) => ({key: name, preventDefault: vi.fn(), stopPropagation: vi.fn(), ...extra});
beforeEach(() => {
    vi.stubGlobal('Vue', {ref: value => ({value}), computed: getter => ({get value() {return getter();}}),
        watch: (_getter, callback) => {disabledChanged = callback;}, nextTick: callback => callback()});
    props = {modelValue: 'Анна', options: ['Анна', 'Борис', 'Вера'], disabled: false};
    emit = vi.fn(); picker = AsrSpeakerInput.setup(props, {emit});
    picker.input.value = {focus: vi.fn()};
});
afterEach(() => vi.unstubAllGlobals());

describe('editable ASR speaker picker', () => {
    it('opens all recording names even when the current name is populated', () => {
        picker.show(); expect(picker.suggestions.value).toEqual(props.options);
        expect(emit).not.toHaveBeenCalled();
        picker.keydown(key('ArrowDown')); picker.keydown(key('ArrowDown')); picker.keydown(key('Enter'));
        expect(emit).toHaveBeenCalledWith('update:modelValue', 'Борис');
        expect(picker.open.value).toBe(false);
    });
    it('filters only after typing and resets the filter when reopened', () => {
        picker.edit({target: {value: 'бО'}});
        expect(picker.suggestions.value).toEqual(['Борис']);
        expect(emit).toHaveBeenCalledWith('update:modelValue', 'бО');
        picker.close(); picker.toggle(); expect(picker.suggestions.value).toEqual(props.options);
    });
    it('retains a free name when no existing speaker matches', () => {
        picker.edit({target: {value: 'Новое имя'}}); picker.keydown(key('Enter'));
        expect(emit).toHaveBeenCalledTimes(1);
        expect(emit).toHaveBeenCalledWith('update:modelValue', 'Новое имя');
        expect(picker.open.value).toBe(false);
    });
    it('supports upward wrap, Escape and Tab without changing a name', () => {
        picker.keydown(key('ArrowUp')); expect(picker.active.value).toBe(2);
        picker.keydown(key('ArrowDown')); expect(picker.active.value).toBe(0);
        picker.keydown(key('Escape')); expect(picker.open.value).toBe(false);
        picker.show(); const tab = key('Tab'); picker.keydown(tab);
        expect(picker.open.value).toBe(false); expect(tab.preventDefault).not.toHaveBeenCalled();
        expect(emit).not.toHaveBeenCalled();
    });
    it('closes on external focus while allowing the toggle to receive focus', () => {
        const button = {}; picker.root.value = {contains: target => target === button};
        picker.show(); picker.blur({relatedTarget: button}); expect(picker.open.value).toBe(true);
        picker.blur({relatedTarget: null}); expect(picker.open.value).toBe(false);
    });
    it('guards mutations and closes suggestions when the editor is disabled', () => {
        picker.show(); props.disabled = true; disabledChanged(true);
        picker.show(); picker.toggle(); picker.edit({target: {value: 'Борис'}}); picker.choose('Борис');
        picker.keydown(key('ArrowDown'));
        expect(picker.open.value).toBe(false); expect(emit).not.toHaveBeenCalled();
    });
    it('does not intercept IME confirmation or unrelated keyboard shortcuts', () => {
        picker.show(); const ime = key('Enter', {isComposing: true}), save = key('s', {ctrlKey: true});
        picker.keydown(ime); picker.keydown(save);
        expect(ime.preventDefault).not.toHaveBeenCalled(); expect(save.preventDefault).not.toHaveBeenCalled();
        expect(emit).not.toHaveBeenCalled();
    });
    it('drops invalid and duplicate names and tolerates an empty recording', () => {
        props.options = ['Анна', '', null, 'Анна', '   ', 'Борис'];
        expect(picker.suggestions.value).toEqual(['Анна', 'Борис']);
        props.options = []; picker.keydown(key('ArrowDown')); picker.keydown(key('Enter'));
        expect(emit).not.toHaveBeenCalled(); expect(picker.open.value).toBe(false);
    });
});
