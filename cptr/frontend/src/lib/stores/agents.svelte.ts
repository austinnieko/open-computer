/**
 * Every task agent across all repositories and folders: the data behind the
 * Agents dashboard, the sidebar's Agents count and Home's one-line summary.
 *
 * Event-driven like the per-repo coordination store: refetched (debounced) on
 * `coord:*` events, when a task chat's turn starts or stops, and on reconnect.
 * Usage (tokens, cost, time) is costlier, so it is only fetched while the
 * dashboard is open.
 */

import { untrack } from 'svelte';
import { goto } from '$app/navigation';
import { get } from 'svelte/store';
import { getCoordSummary, type CoordTask, type TaskUsage } from '$lib/apis/coordination';
import { socketStore } from '$lib/stores/socket.svelte';
import { addWorkspace, currentWorkspace, openCoordinationTab, workspaceList } from '$lib/stores';
import { coordinationFocus, lifecycleOf, type TaskLifecycle } from './coordination.svelte';

/** One reading of a task's state, as used across the agent views. */
export type AgentState =
	| 'needs_you'
	| 'blocked'
	| 'working'
	| 'background'
	| 'in_line'
	| 'stopped'
	| 'review'
	| 'draft'
	| 'done'
	| 'ended';

export interface AgentRow {
	task: CoordTask;
	life: TaskLifecycle;
	state: AgentState;
	/** The folder the task was started from (a checkout or worktree), resolved. */
	folder: string;
	/** The repository's main checkout. */
	repo: string;
	usage: TaskUsage | null;
}

export const STATE_ORDER: Record<AgentState, number> = {
	needs_you: 0,
	blocked: 1,
	working: 2,
	background: 3,
	in_line: 4,
	stopped: 5,
	review: 6,
	draft: 7,
	done: 8,
	ended: 9
};

const state = $state({
	tasks: [] as CoordTask[],
	activeChats: new Set<string>(),
	maxWriters: 2,
	usage: {} as Record<string, TaskUsage>,
	usageTotals: null as TaskUsage | null,
	conflicts: {} as Record<string, number>,
	loaded: false,
	loading: false,
	error: ''
});

let refs = 0;
let usageRefs = 0;
let listening = false;
let timer: ReturnType<typeof setTimeout> | null = null;
let seq = 0;

async function load() {
	const mine = ++seq;
	const withUsage = usageRefs > 0;
	state.loading = true;
	try {
		const data = await getCoordSummary(withUsage);
		if (mine !== seq) return;
		state.tasks = data.tasks;
		state.activeChats = new Set(data.active_chats);
		state.maxWriters = data.limits?.max_writers ?? state.maxWriters;
		if (data.usage) {
			state.usage = data.usage.tasks;
			state.usageTotals = data.usage.totals;
		}
		if (data.conflicts) state.conflicts = data.conflicts;
		state.error = '';
	} catch (error: any) {
		if (mine === seq) state.error = error?.message ?? String(error);
	} finally {
		if (mine === seq) {
			state.loading = false;
			state.loaded = true;
		}
	}
}

function schedule(delay: number) {
	if (refs <= 0) return;
	if (timer) clearTimeout(timer);
	timer = setTimeout(() => {
		timer = null;
		void load();
	}, delay);
}

function ensureListener() {
	if (listening) return;
	listening = true;
	socketStore.on('events:chat', (data: any) => {
		const type = data?.type;
		if (typeof type === 'string' && type.startsWith('coord:')) {
			schedule(400);
		} else if (type === 'chat:active' && state.tasks.some((t) => t.chat_id === data.chat_id)) {
			const next = new Set(state.activeChats);
			if (data.active) next.add(data.chat_id);
			else next.delete(data.chat_id);
			state.activeChats = next;
			if (!data.active) schedule(1500); // a finished turn updates the task
		}
	});
	socketStore.on('connect', () => schedule(100));
}

/** Keep the summary live while a component is mounted; `usage` also fetches usage. */
export function trackAgents(opts: { usage?: boolean } = {}): () => void {
	return untrack(() => {
		ensureListener();
		refs += 1;
		if (opts.usage) usageRefs += 1;
		if (!state.loaded || opts.usage) void load();
		return () =>
			untrack(() => {
				refs -= 1;
				if (opts.usage) usageRefs -= 1;
			});
	});
}

export function refreshAgents(): Promise<void> {
	return load();
}

export function agentsState() {
	return state;
}

const norm = (p: string) => p.replace(/\/+$/, '');
export const folderName = (p: string) => norm(p).split('/').pop() || p;

export function stateOf(life: TaskLifecycle): AgentState {
	return (life.stage === 'active' && life.activity ? life.activity : life.stage) as AgentState;
}

/** Every task as a row, most urgent first. */
export function agentRows(): AgentRow[] {
	const titles = new Map(state.tasks.map((t) => [t.id, t.title]));
	return state.tasks
		.map((task) => {
			const repo = norm(task.repo_root || '');
			const runningWriters = state.tasks.filter(
				(t) => t.repo_key === task.repo_key && t.status === 'running' && t.worktree_path
			).length;
			const life = lifecycleOf(task, { titles, runningWriters, maxWriters: state.maxWriters });
			return {
				task,
				life,
				state: stateOf(life),
				folder: norm(String(task.meta?.origin_root ?? '')) || repo,
				repo,
				usage: state.usage[task.id] ?? null
			};
		})
		.sort(
			(a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || b.task.updated_at - a.task.updated_at
		);
}

/** How many tasks are in each state. */
export function agentCounts(rows: AgentRow[]): Partial<Record<AgentState, number>> {
	const counts: Partial<Record<AgentState, number>> = {};
	for (const r of rows) counts[r.state] = (counts[r.state] ?? 0) + 1;
	return counts;
}

function workspacePath(folder: string): string {
	const ws = get(workspaceList).find((w) => norm(w.path) === norm(folder));
	if (!ws) addWorkspace(folder);
	return ws?.path ?? folder;
}

/** Open a task's chat in the folder it was started from. */
export function openAgentChat(task: CoordTask, folder: string) {
	const path = encodeURIComponent(workspacePath(folder));
	goto(
		task.chat_id && !task.chat_missing
			? `/?workspace=${path}&chatId=${encodeURIComponent(task.chat_id)}`
			: `/?workspace=${path}`
	);
}

/** Open a folder's Agents & tasks panel, on one task when given. */
export async function openAgentsPanel(folder: string, taskId?: string) {
	const path = workspacePath(folder);
	if (get(currentWorkspace)?.path !== path) {
		await goto(`/?workspace=${encodeURIComponent(path)}`);
		// The workspace store switches after navigation; wait briefly for it.
		for (let i = 0; i < 40 && get(currentWorkspace)?.path !== path; i++) {
			await new Promise((r) => setTimeout(r, 50));
		}
	}
	if (get(currentWorkspace)?.path !== path) return;
	if (taskId) coordinationFocus.set(taskId);
	openCoordinationTab();
}
