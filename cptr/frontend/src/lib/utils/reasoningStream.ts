/**
 * Streamed reasoning, applied a piece at a time.
 *
 * While a reasoning item is in progress the server sends only the new text
 * and the offset it starts at (`reasoning_delta`), not the whole item again.
 * The first and final updates still carry the full item. The offset makes a
 * piece that overlaps text already shown (after a reload, say) harmless, and
 * a piece past the end (one was missed) is skipped until the full item lands.
 *
 * Pure TypeScript (no Svelte) so it can be tested directly with Node.
 */

export interface ReasoningDelta {
	id: string;
	offset: number;
	text: string;
}

/** Join two pieces of the same item queued in one frame. */
export function mergeReasoningDelta(
	prev: ReasoningDelta | undefined,
	next: ReasoningDelta
): ReasoningDelta {
	if (!prev) return { ...next };
	const end = prev.offset + prev.text.length;
	if (next.offset > end || next.offset < prev.offset) return { ...next };
	return {
		id: next.id,
		offset: prev.offset,
		text: prev.text.slice(0, next.offset - prev.offset) + next.text
	};
}

/** Apply a piece to the matching in-progress reasoning item. Returns whether it changed. */
export function applyReasoningDelta(
	output: any[] | null | undefined,
	delta: ReasoningDelta
): boolean {
	const item = output?.find((o: any) => o?.type === 'reasoning' && o.id === delta.id);
	if (!item || item.status !== 'in_progress') return false;
	const block = Array.isArray(item.content) && item.content.length === 1 ? item.content[0] : null;
	const current = typeof block?.text === 'string' ? block.text : item.content?.length ? null : '';
	if (current === null || delta.offset > current.length) return false;
	// Already shown (a repeat after a reload): never cut text back.
	if (delta.offset + delta.text.length <= current.length) return false;
	const text = current.slice(0, delta.offset) + delta.text;
	if (block) block.text = text;
	else item.content = [{ type: 'reasoning_text', text }];
	return true;
}
