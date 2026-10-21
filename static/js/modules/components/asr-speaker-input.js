let pickerId = 0;

/** Editable speaker name with an explicit list, independent of native datalist filtering. */
export const AsrSpeakerInput = {
    inheritAttrs: false,
    props: {
        modelValue: {type: String, default: ''},
        options: {type: Array, default: () => []},
        disabled: Boolean,
        label: {type: String, required: true}
    },
    emits: ['update:modelValue'],
    setup(props, {emit}) {
        const {ref, computed, watch, nextTick} = Vue;
        const root = ref(null), input = ref(null), list = ref(null);
        const open = ref(false), query = ref(''), active = ref(-1);
        const listId = `asr-speaker-options-${++pickerId}`;
        const suggestions = computed(() => [...new Set(props.options.filter(name => typeof name === 'string' && name.trim()))]
            .filter(name => name.toLocaleLowerCase().includes(query.value.toLocaleLowerCase())));
        const close = () => {open.value = false; active.value = -1;};
        const show = () => {
            if (props.disabled) return;
            query.value = ''; active.value = -1; open.value = true;
        };
        const toggle = () => {
            if (props.disabled) return;
            if (open.value) close(); else show();
            input.value?.focus();
        };
        const edit = event => {
            if (props.disabled) return;
            query.value = event.target.value; active.value = -1; open.value = true;
            emit('update:modelValue', event.target.value);
        };
        const choose = name => {
            if (props.disabled) return;
            emit('update:modelValue', name); close(); input.value?.focus();
        };
        const keydown = event => {
            if (props.disabled || event.isComposing) return;
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault(); event.stopPropagation();
                if (!open.value) show();
                const count = suggestions.value.length;
                if (count) active.value = active.value < 0 ? (event.key === 'ArrowDown' ? 0 : count - 1) :
                    (active.value + (event.key === 'ArrowDown' ? 1 : -1) + count) % count;
                nextTick(() => list.value?.children[active.value]?.scrollIntoView({block: 'nearest'}));
            } else if (open.value && (event.key === 'Escape' || event.key === 'Enter')) {
                event.preventDefault(); event.stopPropagation();
                if (event.key === 'Enter' && suggestions.value[active.value] !== undefined) choose(suggestions.value[active.value]);
                else close();
            } else if (event.key === 'Tab') close();
        };
        const blur = event => {if (!root.value?.contains(event.relatedTarget)) close();};
        watch(() => props.disabled, disabled => {if (disabled) close();});
        return {root, input, list, open, active, listId, suggestions, close, show, toggle, edit, choose, keydown, blur};
    },
    template: `
      <div ref="root" class="sw-speaker-picker" @focusout="blur" @keydown="keydown">
        <input ref="input" v-bind="$attrs" :value="modelValue" :disabled="disabled" @input="edit" @click="show"
          role="combobox" :aria-label="label" aria-autocomplete="list" aria-haspopup="listbox" :aria-expanded="open"
          :aria-controls="listId" :aria-activedescendant="open && active >= 0 ? listId+'-'+active : undefined" />
        <button type="button" class="sw-speaker-toggle" :disabled="disabled" :aria-label="label"
          :aria-expanded="open" :aria-controls="listId" @mousedown.prevent @click="toggle"><i class="fas fa-chevron-down" aria-hidden="true"></i></button>
        <ul v-if="open && !disabled && suggestions.length" ref="list" :id="listId" role="listbox" :aria-label="label" class="sw-speaker-options custom-scrollbar">
          <li v-for="(name, i) in suggestions" :key="name" :id="listId+'-'+i" role="option"
            :aria-selected="name === modelValue" :class="{'sw-speaker-active': i === active}"
            @mousedown.prevent @click.prevent="choose(name)" v-text="name"></li>
        </ul>
      </div>`
};
