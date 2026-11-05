/** Shared look of agent states across the Agents dashboard's views. */
import type { AgentState } from '$lib/stores/agents.svelte';

/** The dashboard's five groups, in the order work moves through them. */
export type AgentGroup = 'attention' | 'running' | 'waiting' | 'review' | 'done';

export const GROUPS: { key: AgentGroup; states: AgentState[]; icon: string }[] = [
	{ key: 'attention', states: ['needs_you', 'blocked'], icon: 'info' },
	{ key: 'running', states: ['working', 'background'], icon: 'play' },
	{ key: 'waiting', states: ['in_line', 'stopped', 'draft'], icon: 'clock' },
	{ key: 'review', states: ['review'], icon: 'git-diff' },
	{ key: 'done', states: ['done', 'ended'], icon: 'check' }
];

export const groupOf = (s: AgentState): AgentGroup => GROUPS.find((g) => g.states.includes(s))!.key;

/** Solid colour per state (dots, bar segments). */
export const SWATCH: Record<AgentState, string> = {
	needs_you: 'bg-amber-500',
	blocked: 'bg-red-500',
	working: 'bg-blue-500',
	background: 'bg-sky-400',
	in_line: 'bg-slate-400',
	stopped: 'bg-slate-300 dark:bg-slate-600',
	review: 'bg-violet-500',
	draft: 'bg-slate-200 dark:bg-slate-700',
	done: 'bg-emerald-500',
	ended: 'bg-slate-300 dark:bg-slate-600'
};

/** Soft pill per state. */
export const PILL: Record<AgentState, string> = {
	needs_you: 'bg-amber-500/12 text-amber-700 ring-amber-500/25 dark:text-amber-300',
	blocked: 'bg-red-500/12 text-red-700 ring-red-500/25 dark:text-red-300',
	working: 'bg-blue-500/12 text-blue-700 ring-blue-500/25 dark:text-blue-300',
	background: 'bg-sky-500/12 text-sky-700 ring-sky-500/25 dark:text-sky-300',
	in_line: 'bg-slate-500/10 text-slate-600 ring-slate-500/20 dark:text-slate-300',
	stopped: 'bg-slate-500/8 text-slate-500 ring-slate-500/15 dark:text-slate-400',
	review: 'bg-violet-500/12 text-violet-700 ring-violet-500/25 dark:text-violet-300',
	draft: 'bg-transparent text-slate-500 ring-slate-400/40 dark:text-slate-400',
	done: 'bg-emerald-500/12 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
	ended: 'bg-slate-500/8 text-slate-500 ring-slate-500/15 dark:text-slate-400'
};

/** Accent per group (metric cards, board columns). */
export const GROUP_ACCENT: Record<AgentGroup, { text: string; bar: string; glow: string }> = {
	attention: {
		text: 'text-amber-600 dark:text-amber-400',
		bar: 'bg-amber-500',
		glow: 'from-amber-500/12'
	},
	running: {
		text: 'text-blue-600 dark:text-blue-400',
		bar: 'bg-blue-500',
		glow: 'from-blue-500/12'
	},
	waiting: {
		text: 'text-slate-700 dark:text-slate-200',
		bar: 'bg-slate-400',
		glow: 'from-slate-500/10'
	},
	review: {
		text: 'text-violet-600 dark:text-violet-400',
		bar: 'bg-violet-500',
		glow: 'from-violet-500/12'
	},
	done: {
		text: 'text-emerald-600 dark:text-emerald-400',
		bar: 'bg-emerald-500',
		glow: 'from-emerald-500/12'
	}
};

export const ACTION_ICON: Record<string, string> = {
	start: 'play',
	stop: 'stop',
	resume: 'play',
	retry: 'refresh',
	reply: 'chat-bubble',
	review: 'git-diff',
	integrate: 'git-branch',
	delete: 'trash'
};

export function ago(ms: number, now = Date.now()): string {
	const s = Math.max(0, Math.round((now - ms) / 1000));
	if (s < 60) return 'now';
	const m = Math.round(s / 60);
	if (m < 60) return `${m}m`;
	const h = Math.round(m / 60);
	return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`;
}
