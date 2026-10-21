const pending = new WeakMap();

/** Protect an idle restored location, then resume normal anchoring on user input. */
export function restoreScrollTop(container, top) {
    const previous = pending.get(container);
    const original = previous?.original ?? container.style.overflowAnchor;
    previous?.detach();
    const boundary = container.closest('[data-testid="asr-editor"]') || container;
    const events = ['pointerdown', 'click', 'keydown', 'wheel', 'touchstart', 'beforeinput', 'input'];
    const detach = () => events.forEach(type => boundary.removeEventListener(type, release, true));
    const release = () => {
        detach();
        if (pending.get(container) !== token) return;
        if (container.style.overflowAnchor === 'none') container.style.overflowAnchor = original;
        pending.delete(container);
    };
    const token = { original, detach };
    pending.set(container, token);
    container.style.overflowAnchor = 'none';
    container.scrollTop = top;
    // Lazy rows can still change height after several frames. Keep the idle
    // location stable until the user resumes interacting with either pane.
    events.forEach(type => boundary.addEventListener(type, release, {capture:true, passive:true}));
}
