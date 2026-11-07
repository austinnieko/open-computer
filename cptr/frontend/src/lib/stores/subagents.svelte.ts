/**
 * Live sub-agent state per parent chat.
 *
 * Sub-agents run in their own internal chats. Their socket events already
 * reach this browser tagged with the child chat id, so we listen for those
 * (and for delegate_task calls in the parent) and refresh the summary list.
 */

import { untrack } from 'svelte';
import { getSubagents, type SubagentInfo } from '$lib/apis/chat';
import { socketStore } from '$lib/stores/socket.svelte';

interface Entry {
	items: SubagentInfo[];
	loaded: boolean;
	timer: ReturnType<typeof setTimeout> | null;
	refs: number;
}

const entries = $state<Record<string, Entry>>({});
let childToParent: Record<string, string> = {};
let listening = false;

function ensureListener() {
	if (listening) return;
	listening = true;
	socketStore.on('events:chat', (data: any) => {
		const chatId = data?.chat_id;
		if (!chatId) return;
		const parent = childToParent[chatId];
		if (parent && entries[parent]) {
			applyLive(parent, chatId, data);
			schedule(parent, data.done ? 50 : 1500);
			return;
		}
		const entry = entries[chatId];
		if (!entry) return;
		const out = data.output;
		const delegated =
			(out?.type === 'function_call' && out?.name === 'delegate_task') ||
			out?.type === 'function_call_output' ||
			data.async_subagent_pending ||
			data.done;
		if (delegated) schedule(chatId, 300);
	});
	socketStore.on('connect', () => {
		for (const id of Object.keys(entries)) schedule(id, 100);
	});
}

/** Update the one-line activity from a child event without a round trip. */
function applyLive(parent: string, childId: string, data: any) {
	const entry = entries[parent];
	const row = entry.items.find((s) => s.chat_id === childId);
	if (!row) return;
	if (data.done) {
		row.status = data.error ? 'failed' : 'done';
		row.updated_at = Date.now();
		return;
	}
	row.status = 'running';
	const out = data.output;
	if (out?.type === 'function_call') {
		let args = out.arguments;
		if (typeof args === 'string') {
			try {
				args = JSON.parse(args);
			} catch {
				args = {};
			}
		}
		row.activity = { kind: 'tool', name: out.name, status: out.status, arguments: args || {} };
	} else if (out?.type === 'reasoning' || data.reasoning_delta) {
		row.activity = { kind: 'thinking' };
	} else if (typeof data.delta === 'string' && data.delta.trim()) {
		row.activity = { kind: 'writing' };
	}
}

function schedule(chatId: string, delay: number) {
	const entry = entries[chatId];
	if (!entry) return;
	if (entry.timer) clearTimeout(entry.timer);
	entry.timer = setTimeout(() => {
		entry.timer = null;
		void refresh(chatId);
	}, delay);
}

// The sidebar tracks every chat it lists, so a reconnect or a page load asks
// for dozens of lists at once: send a few at a time instead of all together.
const MAX_IN_FLIGHT = 4;
let inFlight = 0;
const waiting: string[] = [];

export function refresh(chatId: string): void {
	if (!entries[chatId] || waiting.includes(chatId)) return;
	waiting.push(chatId);
	pump();
}

function pump() {
	while (inFlight < MAX_IN_FLIGHT && waiting.length) {
		const chatId = waiting.shift()!;
		if (!entries[chatId]) continue;
		inFlight += 1;
		void fetchOne(chatId).finally(() => {
			inFlight -= 1;
			pump();
		});
	}
}

async function fetchOne(chatId: string) {
	try {
		const { subagents } = await getSubagents(chatId);
		const entry = entries[chatId];
		if (!entry) return;
		entry.items = subagents;
		entry.loaded = true;
		for (const s of subagents) childToParent[s.chat_id] = chatId;
	} catch {
		const entry = entries[chatId];
		if (entry) entry.loaded = true;
	}
}

/** Start tracking a parent chat. Returns a release function. */
export function trackSubagents(chatId: string): () => void {
	// Called from effects: never let this read subscribe the caller to `entries`.
	untrack(() => {
		ensureListener();
		if (!entries[chatId]) {
			entries[chatId] = { items: [], loaded: false, timer: null, refs: 0 };
			void refresh(chatId);
		}
		entries[chatId].refs += 1;
	});
	return () => untrack(() => release(chatId));
}

function release(chatId: string) {
	const entry = entries[chatId];
	if (!entry) return;
	entry.refs -= 1;
	if (entry.refs > 0) return;
	if (entry.timer) clearTimeout(entry.timer);
	delete entries[chatId];
	childToParent = Object.fromEntries(
		Object.entries(childToParent).filter(([, parent]) => parent !== chatId)
	);
}

export function subagentsOf(chatId: string | null | undefined): SubagentInfo[] {
	return (chatId && entries[chatId]?.items) || [];
}

export function subagentsLoaded(chatId: string | null | undefined): boolean {
	return !!(chatId && entries[chatId]?.loaded);
}

/** Find the sub-agent started by a delegate_task call. */
export function findSubagent(
	chatId: string | null | undefined,
	call: { call_id?: string; arguments?: any },
	callOutput?: string | null
): SubagentInfo | null {
	const items = subagentsOf(chatId);
	if (!items.length) return null;
	const byCall = items.find((s) => s.call_id && s.call_id === call.call_id);
	if (byCall) return byCall;
	if (callOutput) {
		try {
			const parsed = JSON.parse(callOutput);
			const hit = items.find((s) => s.chat_id === parsed?.subagent_chat_id);
			if (hit) return hit;
		} catch {
			/* plain-text result */
		}
	}
	// Chats from before call ids were recorded: match on the task text.
	let args = call.arguments;
	if (typeof args === 'string') {
		try {
			args = JSON.parse(args);
		} catch {
			args = {};
		}
	}
	const task = (args?.task || '').trim();
	return (task && items.find((s) => !s.call_id && s.task.trim() === task)) || null;
}

/** Short human label for what a sub-agent is doing. */
export function describeActivity(s: SubagentInfo): string {
	if (s.status === 'queued') return 'Waiting to start';
	if (s.status === 'done')
		return s.tool_calls ? `Finished after ${s.tool_calls} steps` : 'Finished';
	if (s.status === 'failed') return 'Failed';
	if (s.status === 'stopped') return 'Stopped';
	const a = s.activity;
	if (!a) return 'Starting…';
	if (a.kind === 'thinking') return 'Thinking…';
	if (a.kind === 'writing') return 'Writing its reply…';
	if (a.status === 'completed')
		return `Done with ${(a.name || 'tool').replace(/_/g, ' ')}, deciding next step…`;
	const args = a.arguments || {};
	const target =
		args.path || args.command || args.query || args.pattern || args.url || args.title || '';
	const name = (a.name || 'tool').replace(/_/g, ' ');
	return target ? `${name}: ${String(target).slice(0, 80)}` : `${name}…`;
}

export function formatElapsed(ms: number): string {
	const total = Math.max(0, Math.round(ms / 1000));
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = total % 60;
	if (h) return `${h}h ${m}m`;
	if (m) return `${m}m ${s.toString().padStart(2, '0')}s`;
	return `${s}s`;
}

/** Timestamps from the API are ms; tolerate seconds or ns just in case. */
export function toMs(t: number | null | undefined): number {
	if (!t) return 0;
	if (t > 1e17) return Math.round(t / 1e6);
	if (t < 1e11) return t * 1000;
	return t;
}

/** The tracked parent chat a sub-agent chat belongs to, if any. */
export function parentOf(chatId: string): string | null {
	return childToParent[chatId] ?? null;
}
