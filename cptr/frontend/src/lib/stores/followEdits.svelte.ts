/**
 * "Follow edits": when on, each file edit an agent makes in the open chat (or
 * one of its sub-agents) opens that file in the editor, scrolled to the change,
 * with the edited lines flashed.
 */

import { get } from 'svelte/store';
import { currentWorkspace, openFileTab, openInSplit, setActiveGroup } from '$lib/stores';
import { socketStore } from '$lib/stores/socket.svelte';
import { parentOf } from '$lib/stores/subagents.svelte';
import { fileEditFromCall, type FileEdit } from '$lib/utils/fileEdits';

// Per-chat setting, saved with the chat's other settings (params.follow_edits).
const enabledChats = new Set<string>();
const watched = new Set<string>();
const pendingCalls = new Map<string, FileEdit>();
const followed = new Set<string>();
let listening = false;
let requestId = 0;

/** Turn following on or off for one chat (and its sub-agents). */
export function setFollowEdits(chatId: string, on: boolean) {
	if (on) enabledChats.add(chatId);
	else enabledChats.delete(chatId);
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

function owningChat(chatId: string): string | null {
	if (watched.has(chatId)) return chatId;
	const parent = parentOf(chatId);
	return parent && watched.has(parent) ? parent : null;
}

function follow(owner: string, callId: string, edit: FileEdit) {
	if (followed.has(callId)) return;
	followed.add(callId);
	if (!enabledChats.has(owner) || !insideWorkspace(edit.path)) return;
	showEdit(edit.path, edit.flashText);
}

/** Only follow into the workspace; agents also write scratch files elsewhere. */
function insideWorkspace(path: string): boolean {
	const ws = (get(currentWorkspace)?.path || '').replace(/\/$/, '');
	const abs = resolveWorkspacePath(path);
	return !!ws && (abs === ws || abs.startsWith(ws + '/'));
}

function ensureListener() {
	if (listening) return;
	listening = true;
	socketStore.on('events:chat', (data: any) => {
		const out = data?.output;
		const owner = out && data.chat_id ? owningChat(data.chat_id) : null;
		if (!owner || !out.call_id) return;
		if (out.type === 'function_call') {
			const edit = fileEditFromCall(out);
			if (!edit) return;
			if (out.status === 'failed') {
				pendingCalls.delete(out.call_id);
				return;
			}
			// Coding agents report completion on the call itself.
			if (edit.native && out.status === 'completed') {
				pendingCalls.delete(out.call_id);
				follow(owner, out.call_id, edit);
			} else {
				pendingCalls.set(out.call_id, edit);
			}
			return;
		}
		if (out.type === 'function_call_output' && pendingCalls.has(out.call_id)) {
			const edit = pendingCalls.get(out.call_id)!;
			pendingCalls.delete(out.call_id);
			if (out.status === 'failed') return;
			if (edit.native || !/^\s*(error:|exception:|traceback)/i.test(String(out.output ?? ''))) {
				follow(owner, out.call_id, edit);
			}
		}
	});
}

/** Follow edits made in this chat and its sub-agents while it is open. */
export function watchChatEdits(chatId: string): () => void {
	ensureListener();
	watched.add(chatId);
	return () => watched.delete(chatId);
}
