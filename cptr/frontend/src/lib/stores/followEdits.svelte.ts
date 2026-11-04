/**
 * "Follow edits": when on, each file edit an agent makes in the open chat (or
 * one of its sub-agents) opens that file in the editor, scrolled to the change,
 * with the edited lines flashed.
 */

import { get } from 'svelte/store';
import { currentWorkspace, openFileTab, openInSplit, setActiveGroup } from '$lib/stores';
import { socketStore } from '$lib/stores/socket.svelte';
import { parentOf } from '$lib/stores/subagents.svelte';

export const FILE_EDIT_TOOLS = new Set([
	'edit_file',
	'multi_edit_file',
	'write_file',
	'create_file'
]);

const STORAGE_KEY = 'cptr.followEdits';

function readPref(): boolean {
	try {
		return localStorage.getItem(STORAGE_KEY) === '1';
	} catch {
		return false;
	}
}

let enabled = $state(readPref());
const watched = new Set<string>();
const pendingCalls = new Map<string, { name: string; args: any }>();
let listening = false;
let requestId = 0;

export const followEdits = {
	get enabled() {
		return enabled;
	},
	set enabled(value: boolean) {
		enabled = value;
		try {
			localStorage.setItem(STORAGE_KEY, value ? '1' : '0');
		} catch {
			/* private mode */
		}
	}
};

function parseArgs(raw: any): any {
	if (typeof raw !== 'string') return raw || {};
	try {
		return JSON.parse(raw);
	} catch {
		return {};
	}
}

/** Text to find and flash in the edited file. */
export function editedText(name: string, args: any): string {
	if (name === 'edit_file') return String(args.replacement ?? '');
	if (name === 'multi_edit_file') {
		const edits = typeof args.edits === 'string' ? parseArgs(args.edits) : args.edits;
		const last = Array.isArray(edits) ? edits[edits.length - 1] : null;
		return String(last?.replacement ?? '');
	}
	return String(args.content ?? '').slice(0, 400);
}

export function resolveWorkspacePath(path: string): string {
	if (!path || path.startsWith('/')) return path;
	const ws = get(currentWorkspace)?.path || '';
	return ws ? `${ws.replace(/\/$/, '')}/${path.replace(/^\.?\/+/, '')}` : path;
}

/**
 * Open a file at an agent's edit and flash the changed lines. The file opens
 * in the pane beside the chat (splitting once if needed) and focus stays on
 * the chat, so following along never takes over what you are doing.
 */
export function showEdit(path: string, text: string) {
	const abs = resolveWorkspacePath(path);
	if (!abs) return;
	requestId += 1;
	const flash = { text, requestId };
	const ws = get(currentWorkspace);
	if (!ws) {
		openFileTab(abs, undefined, { flash });
		return;
	}
	const chatGroupId = ws.activeGroupId;
	let target = ws.groups.find((g) => g.id !== chatGroupId)?.id;
	if (!target) {
		openInSplit(abs);
		target = get(currentWorkspace)?.groups.find((g) => g.id !== chatGroupId)?.id;
	}
	openFileTab(abs, target, { flash });
	setActiveGroup(chatGroupId);
}

function isWatched(chatId: string): boolean {
	if (watched.has(chatId)) return true;
	const parent = parentOf(chatId);
	return !!parent && watched.has(parent);
}

function ensureListener() {
	if (listening) return;
	listening = true;
	socketStore.on('events:chat', (data: any) => {
		const out = data?.output;
		if (!out || !data.chat_id || !isWatched(data.chat_id)) return;
		if (out.type === 'function_call' && FILE_EDIT_TOOLS.has(out.name) && out.call_id) {
			pendingCalls.set(out.call_id, { name: out.name, args: parseArgs(out.arguments) });
			return;
		}
		if (out.type === 'function_call_output' && out.call_id && pendingCalls.has(out.call_id)) {
			const call = pendingCalls.get(out.call_id)!;
			pendingCalls.delete(out.call_id);
			if (!enabled || out.status === 'failed' || !call.args?.path) return;
			if (call.name === 'create_file' && call.args.artifact_type) return;
			showEdit(String(call.args.path), editedText(call.name, call.args));
		}
	});
}

/** Follow edits made in this chat and its sub-agents while it is open. */
export function watchChatEdits(chatId: string): () => void {
	ensureListener();
	watched.add(chatId);
	return () => watched.delete(chatId);
}
