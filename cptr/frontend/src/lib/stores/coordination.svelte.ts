/**
 * Live coordination state per repository root.
 *
 * Event-driven: the backend pushes `coord:*` events on the shared
 * `events:chat` socket channel and `chat:active` when an agent turn starts or
 * stops. We refetch (debounced) on coord events and on reconnect; there is no
 * polling.
 */

import { untrack } from 'svelte';
import { writable } from 'svelte/store';
import {
	getCoordConflicts,
	getCoordIntegrations,
	getCoordMessages,
	getCoordTasks,
	getCoordUsage,
	type CoordConflict,
	type CoordIntegration,
	type CoordMessage,
	type CoordTask,
	type TaskUsage
} from '$lib/apis/coordination';
import { socketStore } from '$lib/stores/socket.svelte';

export interface CoordState {
	loaded: boolean;
	loading: boolean;
	error: string;
	notRepo: boolean;
	repoRoot: string;
	/** The folder this dashboard was opened from (resolved by the server). */
	viewRoot: string;
	tasks: CoordTask[];
	dependencies: { task_id: string; depends_on: string }[];
	conflicts: CoordConflict[];
	messages: CoordMessage[];
	integrations: CoordIntegration[];
	usage: Record<string, TaskUsage>;
	usageTotals: TaskUsage | null;
	activeChats: Set<string>;
	maxWriters: number;
}

interface Entry {
	state: CoordState;
	refs: number;
	timer: ReturnType<typeof setTimeout> | null;
	seq: number;
}

const entries = $state<Record<string, Entry>>({});

/** Task the Agents & tasks dashboard should select next (set by e.g. a chat's task chip). */
export const coordinationFocus = writable<string | null>(null);
let listening = false;

function empty(): CoordState {
	return {
		loaded: false,
		loading: false,
		error: '',
		notRepo: false,
		repoRoot: '',
		viewRoot: '',
		maxWriters: 2,
		tasks: [],
		dependencies: [],
		conflicts: [],
		messages: [],
		integrations: [],
		usage: {},
		usageTotals: null,
		activeChats: new Set()
	};
}

/** How long a folder's state is kept after nothing shows it. */
const CACHE_MS = 10 * 60_000;

function ensureListener() {
	if (listening) return;
	listening = true;
	socketStore.on('events:chat', (data: any) => {
		const type = data?.type;
		if (typeof type === 'string' && type.startsWith('coord:')) {
			// Only folders on screen refetch; a cached one refreshes when shown again.
			for (const [root, entry] of Object.entries(entries)) if (entry.refs > 0) schedule(root, 250);
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
		for (const [root, entry] of Object.entries(entries)) if (entry.refs > 0) schedule(root, 100);
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
	// Not on screen: keep the cached state (and its eviction timer); it
	// refreshes when shown again.
	if (entry.refs <= 0) return;
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
		const [tasks, conflicts, messages, integrations, usage] = await Promise.all([
			getCoordTasks(root),
			getCoordConflicts(root),
			getCoordMessages(root),
			getCoordIntegrations(root),
			getCoordUsage(root).catch(() => null) // usage is optional; never block the dashboard
		]);
		if (seq !== entry.seq) return;
		entry.state.repoRoot = tasks.repo_root;
		entry.state.viewRoot = tasks.root ?? root;
		entry.state.tasks = tasks.tasks;
		if (tasks.limits) entry.state.maxWriters = tasks.limits.max_writers;
		entry.state.dependencies = tasks.dependencies;
		entry.state.activeChats = new Set(tasks.active_chats);
		entry.state.conflicts = conflicts.conflicts;
		entry.state.messages = messages.messages;
		entry.state.integrations = integrations.integrations;
		if (usage) {
			entry.state.usage = usage.tasks;
			entry.state.usageTotals = usage.totals;
		}
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
				// Keep the cached state a while so coming back to this folder (or a
				// remount: tab moved, pane split) shows it at once, then refreshes.
				if (entry.timer) clearTimeout(entry.timer);
				entry.timer = setTimeout(() => {
					if (entries[root] && entries[root].refs <= 0) delete entries[root];
				}, CACHE_MS);
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

/**
 * Status to show for a task. A queued task with a chat, no pending start
 * prompt and nothing blocking it is not waiting on the scheduler: it starts
 * when the user sends a message in its chat, so show that instead of "Queued".
 */
export function displayStatus(task: CoordTask): string {
	if (task.chat_missing && !['completed', 'ended'].includes(task.status)) return 'chat_deleted';
	if (task.status === 'queued' && task.meta?.waiting_for_user && !(task.blockers ?? []).length) {
		// Its agent finished a turn and is idle; the user's next message continues it.
		return 'waiting_for_user';
	}
	if (
		task.status === 'queued' &&
		task.chat_id &&
		!task.meta?.start &&
		!(task.blockers ?? []).length
	) {
		return 'awaiting_input';
	}
	if (task.status === 'queued' && task.meta?.start && !(task.blockers ?? []).length) {
		// Has instructions and nothing blocks it: waiting for an agent slot.
		return 'waiting_slot';
	}
	return task.status;
}

/** Compact number: 950, 12.3K, 4.5M. */
export function compactNumber(n: number): string {
	if (n < 1000) return String(n);
	if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
	return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Working time: 45s, 12m, 1h 20m. */
export function compactDuration(ms: number): string {
	const s = Math.round(ms / 1000);
	if (s < 60) return `${s}s`;
	const m = Math.round(s / 60);
	if (m < 60) return `${m}m`;
	return `${Math.floor(m / 60)}h ${m % 60}m`;
}

// ── Lifecycle ───────────────────────────────────────────────
// One reading of a task, used by the dashboard and the sidebar:
//   stage    - where the task is in its life (Draft → Active → Review → Done)
//   activity - what is happening right now, while it is Active
//   action   - the one thing to do next (the main button)
// It is derived from the stored status, blockers and meta (see the server's
// cptr/utils/coordination/lifecycle.py for how those are set).

export type TaskStage = 'draft' | 'active' | 'review' | 'done' | 'ended';
export type TaskActivity =
	| 'working'
	| 'in_line'
	| 'background'
	| 'needs_you'
	| 'blocked'
	| 'stopped'
	| null;
export type TaskAction =
	| 'start'
	| 'stop'
	| 'reply'
	| 'review'
	| 'integrate'
	| 'resume'
	| 'retry'
	| 'delete'
	| null;

export interface TaskLifecycle {
	stage: TaskStage;
	activity: TaskActivity;
	/** i18n key under coordination.detail.*, or null. */
	detailKey: string | null;
	detailArgs?: Record<string, string | number>;
	/** Free text from the agent or the server (its question, reason or error). */
	detailText?: string;
	action: TaskAction;
}

export interface TaskCheckout {
	kind: 'done' | 'needs-input' | 'blocked' | 'continuing' | 'none';
	note?: string;
	at?: number;
	stalled?: boolean;
}

export function checkoutOf(task: CoordTask): TaskCheckout | null {
	const checkout = task.meta?.checkout as TaskCheckout | undefined;
	return checkout && typeof checkout === 'object' ? checkout : null;
}

export function lifecycleOf(
	task: CoordTask,
	ctx: {
		titles: Map<string, string>;
		runningWriters: number;
		maxWriters: number;
		/** Ids of tasks this one depends on that were ended or failed. */
		goneDeps?: string[];
	}
): TaskLifecycle {
	const blockers = task.blockers ?? [];
	const names = (ids: string[] = []) => ids.map((id) => ctx.titles.get(id) ?? id).join(', ');
	if (task.status === 'completed')
		return { stage: 'done', activity: null, detailKey: null, action: null };
	if (task.status === 'ended')
		return { stage: 'ended', activity: null, detailKey: null, action: null };
	if (task.chat_missing) {
		return { stage: 'active', activity: 'needs_you', detailKey: 'chatGone', action: 'delete' };
	}
	if (task.status === 'reviewing') {
		return { stage: 'review', activity: null, detailKey: 'review', action: 'review' };
	}
	if (task.status === 'integration_ready') {
		return { stage: 'review', activity: null, detailKey: 'passedIntegration', action: 'integrate' };
	}
	if (task.status === 'failed') {
		const error = String(task.meta?.error ?? task.meta?.status_reason ?? '');
		return {
			stage: 'active',
			activity: 'needs_you',
			detailKey: 'failed',
			detailText: error || undefined,
			action: 'retry'
		};
	}
	if (task.status === 'paused') {
		return { stage: 'active', activity: 'stopped', detailKey: 'stopped', action: 'resume' };
	}
	if (task.status === 'running') {
		return { stage: 'active', activity: 'working', detailKey: null, action: 'stop' };
	}
	const failedDep = blockers.find(
		(b) =>
			b.kind === 'dependency_failed' || (b.kind === 'conflict' && b.detail === 'dependency_failed')
	);
	if (failedDep || ctx.goneDeps?.length) {
		return {
			stage: 'active',
			activity: 'needs_you',
			detailKey: 'dependencyGone',
			detailArgs: {
				tasks:
					names(failedDep?.tasks?.length ? failedDep.tasks : ctx.goneDeps) ||
					(failedDep?.kind === 'dependency_failed' ? failedDep.detail : '') ||
					'A dependency'
			},
			action: null
		};
	}
	const waitingOn = blockers.find((b) => b.kind === 'waiting_on');
	const started = !!task.meta?.milestones?.started || !!task.meta?.waiting_for_user;
	const inLine = !!task.meta?.start;
	if (task.status === 'blocked' || waitingOn) {
		const other = blockers.filter((b) => b.kind !== 'waiting_on');
		return {
			stage: started || inLine || task.status === 'blocked' ? 'active' : 'draft',
			activity: started || inLine || task.status === 'blocked' ? 'blocked' : null,
			detailKey: waitingOn ? 'waitingOn' : 'blocked',
			detailArgs: waitingOn ? { tasks: names(waitingOn.tasks) || waitingOn.detail || '' } : {},
			detailText: waitingOn
				? undefined
				: other.map((b) => b.detail || b.kind).join('; ') || undefined,
			action: started || inLine ? 'stop' : 'start'
		};
	}
	// status === 'queued'
	if (task.meta?.waiting_for_user) {
		const checkout = checkoutOf(task);
		const kind = checkout?.stalled ? 'stalled' : (checkout?.kind ?? 'none');
		const key =
			kind === 'needs-input'
				? 'question'
				: kind === 'blocked'
					? 'agentBlocked'
					: kind === 'stalled'
						? 'stalled'
						: 'noReport';
		return {
			stage: 'active',
			// "continuing" with nothing visible running: the agent's own background
			// work (e.g. an OpenCode sub-agent) is invisible here, so don't alarm.
			activity: kind === 'stalled' ? 'background' : 'needs_you',
			detailKey: key,
			detailText: checkout?.note || undefined,
			action: 'reply'
		};
	}
	if (inLine) {
		return {
			stage: 'active',
			activity: 'in_line',
			detailKey: 'inLine',
			detailArgs: { running: ctx.runningWriters, max: ctx.maxWriters },
			action: 'stop'
		};
	}
	return { stage: 'draft', activity: null, detailKey: 'draft', action: 'start' };
}

/** Sidebar badge tone for a task's lifecycle. */
export function lifecycleTone(life: TaskLifecycle): string {
	if (life.stage === 'review') return 'review';
	if (life.stage === 'done' || life.stage === 'ended') return 'done';
	if (life.stage === 'draft' || !life.activity) return 'neutral';
	const tones: Record<NonNullable<TaskActivity>, string> = {
		working: 'running',
		in_line: 'waiting',
		background: 'waiting',
		needs_you: 'attention',
		blocked: 'failed',
		stopped: 'neutral'
	};
	return tones[life.activity];
}
