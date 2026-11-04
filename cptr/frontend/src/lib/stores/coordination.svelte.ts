/**
 * Live coordination state per repository root.
 *
 * Event-driven: the backend pushes `coord:*` events on the shared
 * `events:chat` socket channel and `chat:active` when an agent turn starts or
 * stops. We refetch (debounced) on coord events and on reconnect; there is no
 * polling.
 */

import { untrack } from 'svelte';
import {
	getCoordConflicts,
	getCoordIntegrations,
	getCoordMessages,
	getCoordTasks,
	type CoordConflict,
	type CoordIntegration,
	type CoordMessage,
	type CoordTask
} from '$lib/apis/coordination';
import { socketStore } from '$lib/stores/socket.svelte';

export interface CoordState {
	loaded: boolean;
	loading: boolean;
	error: string;
	notRepo: boolean;
	repoRoot: string;
	tasks: CoordTask[];
	dependencies: { task_id: string; depends_on: string }[];
	conflicts: CoordConflict[];
	messages: CoordMessage[];
	integrations: CoordIntegration[];
	activeChats: Set<string>;
}

interface Entry {
	state: CoordState;
	refs: number;
	timer: ReturnType<typeof setTimeout> | null;
	seq: number;
}

const entries = $state<Record<string, Entry>>({});
let listening = false;

function empty(): CoordState {
	return {
		loaded: false,
		loading: false,
		error: '',
		notRepo: false,
		repoRoot: '',
		tasks: [],
		dependencies: [],
		conflicts: [],
		messages: [],
		integrations: [],
		activeChats: new Set()
	};
}

function ensureListener() {
	if (listening) return;
	listening = true;
	socketStore.on('events:chat', (data: any) => {
		const type = data?.type;
		if (typeof type === 'string' && type.startsWith('coord:')) {
			for (const root of Object.keys(entries)) schedule(root, 250);
			return;
		}
		if (type === 'chat:active' && data.chat_id) {
			for (const entry of Object.values(entries)) {
				if (!entry.state.tasks.some((t) => t.chat_id === data.chat_id)) continue;
				const next = new Set(entry.state.activeChats);
				if (data.active) next.add(data.chat_id);
				else next.delete(data.chat_id);
				entry.state.activeChats = next;
				// A finished turn triggers a backend scan; pick up its results.
				if (!data.active) schedule(entry.state.repoRoot || '', 1500);
			}
		}
	});
	socketStore.on('connect', () => {
		for (const root of Object.keys(entries)) schedule(root, 100);
	});
}

function schedule(root: string, delay: number) {
	const entry = entries[root];
	if (!entry) {
		// repoRoot may differ from the workspace path; find by repo root.
		for (const [key, e] of Object.entries(entries)) {
			if (e.state.repoRoot === root) schedule(key, delay);
		}
		return;
	}
	if (entry.timer) clearTimeout(entry.timer);
	entry.timer = setTimeout(() => {
		entry.timer = null;
		void refresh(root);
	}, delay);
}

export async function refresh(root: string): Promise<void> {
	const entry = entries[root];
	if (!entry) return;
	const seq = ++entry.seq;
	entry.state.loading = true;
	try {
		const [tasks, conflicts, messages, integrations] = await Promise.all([
			getCoordTasks(root),
			getCoordConflicts(root),
			getCoordMessages(root),
			getCoordIntegrations(root)
		]);
		if (seq !== entry.seq) return;
		entry.state.repoRoot = tasks.repo_root;
		entry.state.tasks = tasks.tasks;
		entry.state.dependencies = tasks.dependencies;
		entry.state.activeChats = new Set(tasks.active_chats);
		entry.state.conflicts = conflicts.conflicts;
		entry.state.messages = messages.messages;
		entry.state.integrations = integrations.integrations;
		entry.state.error = '';
		entry.state.notRepo = false;
	} catch (error: any) {
		if (seq !== entry.seq) return;
		const message = error?.message ?? String(error);
		entry.state.notRepo = error?.status === 400 && /git repository/i.test(message);
		entry.state.error = entry.state.notRepo ? '' : message;
	} finally {
		if (seq === entry.seq) {
			entry.state.loading = false;
			entry.state.loaded = true;
		}
	}
}

/** Start tracking a workspace root; returns a release function (ref-counted). */
export function trackCoordination(root: string): () => void {
	// Called from components' $effect: never let the effect depend on store state,
	// or every refresh would re-run the effect and refetch again.
	return untrack(() => {
		ensureListener();
		if (!entries[root]) {
			entries[root] = { state: empty(), refs: 0, timer: null, seq: 0 };
			void refresh(root);
		} else if (entries[root].refs <= 0) {
			// Re-attached within the grace period: cancel the pending eviction, refresh.
			if (entries[root].timer) clearTimeout(entries[root].timer!);
			entries[root].timer = null;
			void refresh(root);
		}
		entries[root].refs += 1;
		return () =>
			untrack(() => {
				const entry = entries[root];
				if (!entry) return;
				entry.refs -= 1;
				if (entry.refs > 0) return;
				// Keep the cached state briefly so a remount (tab moved, pane split)
				// shows data immediately instead of an empty list.
				if (entry.timer) clearTimeout(entry.timer);
				entry.timer = setTimeout(() => {
					if (entries[root] && entries[root].refs <= 0) delete entries[root];
				}, 30_000);
			});
	});
}

export function coordinationOf(root: string): CoordState {
	return entries[root]?.state ?? empty();
}

const LEVEL_RANK: Record<string, number> = { info: 0, potential: 1, confirmed: 2, blocking: 3 };

export function worstConflict(conflicts: CoordConflict[], taskId: string): CoordConflict | null {
	let worst: CoordConflict | null = null;
	for (const c of conflicts) {
		if (c.status === 'resolved' || (c.task_a !== taskId && c.task_b !== taskId)) continue;
		if (!worst || LEVEL_RANK[c.level] > LEVEL_RANK[worst.level]) worst = c;
	}
	return worst;
}

export function levelRank(level: string): number {
	return LEVEL_RANK[level] ?? 0;
}
