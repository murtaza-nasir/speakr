/**
 * The turns of a per-response Word download.
 *
 * A download of one answer holds that answer and the user question it
 * answers: the nearest earlier user turn with text. The recording chat and
 * the Inquire page both use it.
 */

const hasText = (message) => !!message && typeof message.content === 'string' && message.content !== '';

/**
 * The question and the answer at index, as [question, answer], or [answer]
 * when no earlier user turn has text. Empty when the message at index is not
 * an assistant answer with text.
 *
 * @param {Array<object>} messages - the chat messages in screen order
 * @param {number} index - the position of the answer in messages
 * @returns {Array<object>} the message objects themselves, not copies
 */
export function responseWithQuestion(messages, index) {
    if (!Array.isArray(messages)) return [];
    const answer = messages[index];
    if (!hasText(answer) || answer.role !== 'assistant') return [];
    for (let i = index - 1; i >= 0; i--) {
        const message = messages[i];
        if (hasText(message) && message.role === 'user') return [message, answer];
    }
    return [answer];
}
