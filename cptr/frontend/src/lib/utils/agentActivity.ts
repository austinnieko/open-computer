/**
 * Turn an agent's raw steps (reasoning items and tool calls, from Open
 * Computer's own tools or from coding agents such as OpenCode, whose steps
 * arrive as `agent_tool` calls) into readable activity.
 *
 * Everything here is derived from what actually happened: the tool's name and
 * its command or path. Nothing is guessed about intent. A step we can't place
 * is described by its tool, not given an invented milestone.
 *
 * Pure TypeScript (no Svelte) so it can be tested directly with Node.
 */

export type ActivityKind =
	| 'inspect'
	| 'read'
	| 'read_tests'
	| 'search'
	| 'write'
	| 'test'
	| 'build'
	| 'install'
	| 'git_review'
	| 'git_commit'
	| 'git_push'
	| 'git_branch'
	| 'pr'
	| 'issue'
	| 'linear'
	| 'web'
	| 'subagent'
	| 'plan'
	| 'shell'
	| 'other'
	| 'thinking';

export type StepStatus = 'active' | 'done' | 'failed' | 'approval';

/** One tool call, normalised across Open Computer tools and coding agents. */
export interface NormalCall {
	id: string;
	/** Lower-case tool name: run_command, bash, shell, read, edit, task, ... */
	tool: string;
	input: Record<string, any>;
	/** A per-call title the agent gave (OpenCode 1.x `state.title`), if any. */
	title: string;
	status: StepStatus;
	/** Start/end in ms when the agent reports them (OpenCode 1.x). */
	startedAt?: number;
	endedAt?: number;
}

export interface Classified {
	kind: ActivityKind;
	/** Who did it: a sub-agent's type for `task`, else undefined (the main agent). */
	owner?: string;
	/** A short, redacted description of this one call (command, path, query). */
	detail: string;
}

export interface ActivityRun {
	kind: ActivityKind;
	/** Present tense while running, past once done: "Running tests" / "Ran tests". */
	label: string;
	owner?: string;
	status: StepStatus;
	/** The calls in this run, in order (raw items, for the detail view). */
	calls: any[];
	/** Reasoning items that led into this run, in order. */
	reasoning: any[];
	/** Every item of the run (reasoning and calls) in original order. */
	entries: any[];
	failed: number;
	/** The latest call's short description: what it's doing right now. */
	latest: string;
	startedAt?: number;
	endedAt?: number;
}

export interface TodoItem {
	content: string;
	status: 'pending' | 'in_progress' | 'completed' | 'cancelled';
}

// ── Normalising ─────────────────────────────────────────────

function parse(value: unknown): any {
	if (typeof value !== 'string') return value;
	try {
		return JSON.parse(value);
	} catch {
		return value;
	}
}

function callStatus(item: any, output: any): StepStatus {
	const status = String(item?.status || '').toLowerCase();
	if (status === 'pending') return 'approval';
	if (status === 'failed' || status === 'rejected' || output?.status === 'failed') return 'failed';
	if (status === 'completed' || status === 'done') return 'done';
	// Open Computer's own tools: done once their output has arrived.
	if (item?.name !== 'agent_tool' && output) {
		const text = typeof output.output === 'string' ? output.output : '';
		return /^\s*(error:|exception:|traceback)/i.test(text) ? 'failed' : 'done';
	}
	return 'active';
}

export function normalizeCall(item: any, output?: any): NormalCall {
	const args = parse(item?.arguments) || {};
	const isAgent = item?.name === 'agent_tool' || item?.native_agent;
	const state = isAgent && typeof args.state === 'object' && args.state ? args.state : {};
	const input = parse(isAgent ? (args.input ?? state.input) : args) || {};
	const time = typeof state.time === 'object' && state.time ? state.time : {};
	return {
		id: String(item?.call_id || item?.id || ''),
		tool: String(isAgent ? args.title || 'agent tool' : item?.name || 'tool')
			.trim()
			.toLowerCase(),
		input: typeof input === 'object' && input ? input : { value: input },
		title: typeof state.title === 'string' ? state.title : '',
		status: callStatus(item, output),
		startedAt: typeof time.start === 'number' ? time.start : undefined,
		endedAt: typeof time.end === 'number' ? time.end : undefined
	};
}

// ── Redaction ───────────────────────────────────────────────

const SECRET_PATTERNS: [RegExp, string][] = [
	[/(authorization:\s*(?:bearer|basic|token)\s+)[^\s"']+/gi, '$1•••'],
	[/(bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, '$1•••'],
	[/\b(gh[pousr]_|github_pat_)[A-Za-z0-9_]{10,}/g, '$1•••'],
	[/\b(sk-|sk_live_|sk_test_|xox[abpr]-|AKIA)[A-Za-z0-9_-]{8,}/g, '$1•••'],
	[
		/((?:api[_-]?key|token|secret|password|passwd|pwd|access[_-]?key)["']?\s*[=:]\s*["']?)[^\s"'&]+/gi,
		'$1•••'
	],
	[/(--(?:password|token|api-key|secret)[= ])[^\s]+/gi, '$1•••'],
	[/([a-z][a-z0-9+.-]*:\/\/[^\s:/@]+:)[^\s@/]+@/gi, '$1•••@']
];

/** Mask tokens, passwords and keys in text meant for a summary. */
export function redact(text: string): string {
	let out = text;
	for (const [pattern, replacement] of SECRET_PATTERNS) out = out.replace(pattern, replacement);
	return out;
}

function short(text: string, max = 90): string {
	const one = redact(text.replace(/\s+/g, ' ').trim());
	return one.length > max ? one.slice(0, max - 1) + '…' : one;
}

function basename(path: string): string {
	const parts = String(path || '')
		.split('/')
		.filter(Boolean);
	return parts.at(-1) || path || '';
}

// ── Classifying ─────────────────────────────────────────────

const READ_TOOLS = new Set(['read', 'read_file', 'view', 'cat', 'display_file']);
const LIST_TOOLS = new Set(['list', 'ls', 'list_directory', 'tree']);
const SEARCH_TOOLS = new Set([
	'grep',
	'glob',
	'search',
	'search_files',
	'codesearch',
	'find',
	'ripgrep'
]);
const WRITE_TOOLS = new Set([
	'write',
	'write_file',
	'create_file',
	'edit',
	'edit_file',
	'multiedit',
	'multi_edit',
	'multi_edit_file',
	'patch',
	'apply_patch',
	'str_replace',
	'str_replace_editor'
]);
const SHELL_TOOLS = new Set(['bash', 'shell', 'run_command', 'exec', 'command', 'terminal']);
const WEB_TOOLS = new Set([
	'webfetch',
	'web_fetch',
	'websearch',
	'web_search',
	'read_url',
	'fetch'
]);
const SUBAGENT_TOOLS = new Set(['task', 'delegate_task', 'agent', 'subagent']);
const PLAN_TOOLS = new Set(['todowrite', 'todo_write', 'todoread', 'todo_read', 'update_plan']);

const TEST_PATH = /(^|\/)(tests?|__tests__|spec)(\/|$)|[._-](test|spec)\.[a-z]+$|^test_.*\.py$/i;

function pathOf(input: Record<string, any>): string {
	for (const key of ['filePath', 'file_path', 'path', 'file', 'filename', 'target_file']) {
		if (typeof input[key] === 'string' && input[key]) return input[key];
	}
	return '';
}

/** Classify a shell command by its first meaningful program and arguments. */
function classifyCommand(command: string): ActivityKind {
	// Look at each command in a pipeline / chain; the most significant wins.
	const parts = command
		.split(/&&|\|\||;|\|/)
		.map((p) => p.trim().replace(/^(sudo|env\s+\S+=\S+|cd\s+\S+)\s+/, ''))
		.filter(Boolean);
	const kinds = parts.map(classifyOne);
	const order: ActivityKind[] = [
		'pr',
		'issue',
		'linear',
		'git_push',
		'git_commit',
		'git_branch',
		'test',
		'build',
		'install',
		'git_review',
		'web',
		'search',
		'read_tests',
		'read',
		'inspect',
		'shell'
	];
	for (const kind of order) if (kinds.includes(kind)) return kind;
	return 'shell';
}

function classifyOne(cmd: string): ActivityKind {
	const c = cmd.trim();
	if (/^gh\s+pr\b/.test(c)) return 'pr';
	if (/^gh\s+issue\b/.test(c)) return 'issue';
	if (/^linear\b/.test(c)) return 'linear';
	if (/^git\s+push\b/.test(c)) return 'git_push';
	if (/^git\s+(add|commit|stash|reset|restore|revert|cherry-pick|merge|rebase|am|apply)\b/.test(c))
		return 'git_commit';
	if (/^git\s+(checkout|switch|branch|worktree|fetch|pull|tag)\b/.test(c)) return 'git_branch';
	if (/^git\s+(diff|status|log|show|blame|grep|ls-files|rev-parse|describe)\b/.test(c))
		return 'git_review';
	if (
		/\b(pytest|unittest|vitest|jest|mocha|playwright\s+test|cypress\s+run|go\s+test|cargo\s+test|rspec|phpunit|tox|nox)\b/.test(
			c
		) ||
		/\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b/.test(c) ||
		/\buv\s+run\s+(python\s+-m\s+)?(pytest|unittest)\b/.test(c) ||
		/\bpython3?\s+-m\s+(pytest|unittest)\b/.test(c)
	)
		return 'test';
	if (
		/\b(npm|pnpm|yarn|bun)\s+(run\s+)?(build|lint|check|typecheck|format)\b/.test(c) ||
		/\b(tsc|svelte-check|vite\s+build|eslint|prettier|ruff|mypy|pyright|cargo\s+(build|check|clippy)|go\s+(build|vet)|make)\b/.test(
			c
		)
	)
		return 'build';
	if (
		/\b(npm|pnpm|yarn|bun)\s+(install|i|add|ci)\b/.test(c) ||
		/\b(pip3?|uv\s+pip)\s+install\b/.test(c) ||
		/\buv\s+(sync|add)\b/.test(c) ||
		/\b(cargo\s+add|go\s+get|brew\s+install|apt(-get)?\s+install)\b/.test(c)
	)
		return 'install';
	if (/^(curl|wget|http)\b/.test(c) && /https?:\/\//.test(c)) return 'web';
	if (/^(rg|grep|ag|ack|fd|git\s+grep)\b/.test(c) || /^find\b.*-(i?name|path|regex)\b/.test(c))
		return 'search';
	if (/^(cat|head|tail|less|more|bat|sed\s+-n|nl|awk)\b/.test(c)) {
		const files = c
			.split(/\s+/)
			.slice(1)
			.filter((a) => !a.startsWith('-'));
		return files.length && files.every((f) => TEST_PATH.test(f)) ? 'read_tests' : 'read';
	}
	if (/^(ls|tree|pwd|find|du|stat|file|wc|realpath|readlink|exa|eza)\b/.test(c)) return 'inspect';
	return 'shell';
}

export function classify(call: NormalCall): Classified {
	const { tool, input } = call;
	const command = typeof input.command === 'string' ? input.command : '';
	const path = pathOf(input);

	if (PLAN_TOOLS.has(tool)) return { kind: 'plan', detail: 'Updated the plan' };
	if (SUBAGENT_TOOLS.has(tool)) {
		const owner = String(input.subagent_type || input.agent || input.subagent || '') || undefined;
		const what = String(input.description || input.task || input.prompt || '');
		return { kind: 'subagent', owner, detail: short(what || 'Sub-agent task', 80) };
	}
	if (WRITE_TOOLS.has(tool)) return { kind: 'write', detail: basename(path) || short(call.title) };
	if (READ_TOOLS.has(tool))
		return { kind: TEST_PATH.test(path) ? 'read_tests' : 'read', detail: basename(path) };
	if (LIST_TOOLS.has(tool)) return { kind: 'inspect', detail: path || '.' };
	if (SEARCH_TOOLS.has(tool)) {
		const q = String(input.pattern || input.query || input.regex || input.glob || '');
		return { kind: 'search', detail: short(q || path || 'search', 60) };
	}
	if (WEB_TOOLS.has(tool)) {
		const target = String(input.url || input.query || '');
		let shown = target;
		try {
			shown = target.startsWith('http') ? new URL(target).hostname : target;
		} catch {
			// not a URL
		}
		return { kind: 'web', detail: short(shown || 'web', 60) };
	}
	if (tool.includes('linear')) return { kind: 'linear', detail: short(call.title || tool, 60) };
	if (SHELL_TOOLS.has(tool) || command) {
		const description = typeof input.description === 'string' ? input.description : '';
		return {
			kind: command ? classifyCommand(command) : 'shell',
			detail: short(command || description || call.title || 'command')
		};
	}
	// MCP and other tools: say which tool, factually.
	return { kind: 'other', detail: short(call.title || tool, 60) };
}

// ── Labels ──────────────────────────────────────────────────

const LABELS: Record<ActivityKind, [active: string, done: string]> = {
	inspect: ['Inspecting repository structure', 'Inspected repository structure'],
	read: ['Reading files', 'Read files'],
	read_tests: ['Examining existing tests', 'Examined existing tests'],
	search: ['Searching the codebase', 'Searched the codebase'],
	write: ['Implementing code changes', 'Made code changes'],
	test: ['Running tests', 'Ran tests'],
	build: ['Building and checking', 'Built and checked'],
	install: ['Installing dependencies', 'Installed dependencies'],
	git_review: ['Reviewing changes', 'Reviewed changes'],
	git_commit: ['Committing changes', 'Committed changes'],
	git_push: ['Pushing changes', 'Pushed changes'],
	git_branch: ['Managing branches', 'Managed branches'],
	pr: ['Preparing pull request', 'Worked on pull request'],
	issue: ['Updating GitHub issue', 'Updated GitHub issue'],
	linear: ['Updating Linear', 'Updated Linear'],
	web: ['Reading the web', 'Read from the web'],
	subagent: ['Sub-agent working', 'Sub-agent finished'],
	plan: ['Updating plan', 'Updated plan'],
	shell: ['Running commands', 'Ran commands'],
	other: ['Using tools', 'Used tools'],
	thinking: ['Thinking', 'Thought']
};

export function runLabel(
	kind: ActivityKind,
	status: StepStatus,
	run?: Partial<ActivityRun>
): string {
	if (kind === 'subagent' && run?.latest) {
		const who = run.owner ? `${capitalize(run.owner)} sub-agent` : 'Sub-agent';
		return `${who}: ${run.latest}`;
	}
	if (kind === 'other' && run?.calls?.length) {
		// Name the tool instead of a vague "Using tools".
		const tool = normalizeCall(run.calls[0]).tool;
		return status === 'active' ? `Using ${tool}` : `Used ${tool}`;
	}
	if (status === 'approval') return LABELS[kind][0];
	return LABELS[kind][status === 'active' ? 0 : 1];
}

function capitalize(s: string): string {
	return s ? s[0].toUpperCase() + s.slice(1) : s;
}

// ── Grouping ────────────────────────────────────────────────

function runStatus(statuses: StepStatus[], done: boolean): StepStatus {
	if (statuses.includes('approval')) return 'approval';
	if (!done && statuses.includes('active')) return 'active';
	// A run whose last step failed shows as failed; earlier failures are counted.
	if (statuses.at(-1) === 'failed') return 'failed';
	return 'done';
}

/**
 * Group an activity block (reasoning items and calls, in order) into runs of
 * the same kind of work. Reasoning attaches to the run it led into; trailing
 * reasoning is its own "Thinking" run while the turn is still going.
 *
 * Unrelated work never merges: a run breaks whenever the kind changes, and
 * each sub-agent call is its own run.
 */
export function buildRuns(entries: any[], outputs: Map<string, any>, done: boolean): ActivityRun[] {
	const runs: ActivityRun[] = [];
	let pendingReasoning: any[] = [];
	let current: (ActivityRun & { statuses: StepStatus[] }) | null = null;

	const finish = () => {
		if (!current) return;
		current.status = runStatus(current.statuses, done);
		current.label = runLabel(current.kind, current.status, current);
		const { statuses: _s, ...run } = current;
		runs.push(run);
		current = null;
	};

	for (const item of entries) {
		if (item?.type === 'reasoning') {
			pendingReasoning.push(item);
			continue;
		}
		if (item?.type !== 'function_call') continue;
		const out = outputs.get(item.call_id);
		const call = normalizeCall(item, out);
		const c = classify(call);
		const sameRun =
			current &&
			current.kind === c.kind &&
			c.kind !== 'subagent' &&
			c.kind !== 'plan' &&
			current.owner === c.owner;
		if (!sameRun) {
			finish();
			current = {
				kind: c.kind,
				label: '',
				owner: c.owner,
				status: 'active',
				calls: [],
				reasoning: [],
				entries: [],
				failed: 0,
				latest: '',
				startedAt: call.startedAt,
				endedAt: call.endedAt,
				statuses: []
			};
		}
		const run = current!;
		run.reasoning.push(...pendingReasoning);
		run.entries.push(...pendingReasoning, item);
		pendingReasoning = [];
		run.calls.push(item);
		run.statuses.push(call.status);
		if (call.status === 'failed') run.failed += 1;
		run.latest = c.detail;
		if (call.startedAt !== undefined && run.startedAt === undefined) run.startedAt = call.startedAt;
		if (call.endedAt !== undefined) run.endedAt = call.endedAt;
	}
	finish();

	if (pendingReasoning.length) {
		// Reasoning after the last call: still thinking while the turn runs;
		// once it's over, it belongs with the last thing it did.
		if (!done) {
			runs.push({
				kind: 'thinking',
				label: 'Thinking',
				status: 'active',
				calls: [],
				reasoning: pendingReasoning,
				entries: pendingReasoning,
				failed: 0,
				latest: ''
			});
		} else if (runs.length) {
			const last = runs[runs.length - 1];
			last.reasoning.push(...pendingReasoning);
			last.entries.push(...pendingReasoning);
		} else {
			runs.push({
				kind: 'thinking',
				label: 'Thought',
				status: 'done',
				calls: [],
				reasoning: pendingReasoning,
				entries: pendingReasoning,
				failed: 0,
				latest: ''
			});
		}
	}
	return runs;
}

/** What's happening now: the latest running work, or "Thinking" only when nothing else runs. */
export function currentRun(runs: ActivityRun[]): ActivityRun | undefined {
	return (
		runs.findLast(
			(r) => (r.status === 'active' || r.status === 'approval') && r.kind !== 'thinking'
		) ?? runs.findLast((r) => r.status === 'active')
	);
}

/** One line for a finished block: "Inspected repository structure, ran tests · 12 operations". */
export function summarizeRuns(runs: ActivityRun[]): string {
	const work = runs.filter((r) => r.kind !== 'thinking' && r.kind !== 'plan');
	if (!work.length) return runs.some((r) => r.kind === 'thinking') ? 'Thought' : '';
	const labels: string[] = [];
	for (const r of work) {
		const l = runLabel(r.kind, 'done', r);
		if (!labels.includes(l)) labels.push(l);
	}
	const shown = labels.slice(0, 3).map((l, i) => (i === 0 ? l : l[0].toLowerCase() + l.slice(1)));
	const more = labels.length > 3 ? ` +${labels.length - 3} more` : '';
	return shown.join(', ') + more;
}

// ── Plan (todo list) ────────────────────────────────────────

/**
 * The agent's own plan, if it keeps one (OpenCode's todowrite): the latest
 * todo list it wrote. These are the agent's real milestones, not guesses.
 */
export function latestTodos(output: any[] | null | undefined): TodoItem[] | null {
	const items = output || [];
	for (let i = items.length - 1; i >= 0; i--) {
		const item = items[i];
		if (item?.type !== 'function_call') continue;
		const call = normalizeCall(item);
		if (call.tool !== 'todowrite' && call.tool !== 'todo_write' && call.tool !== 'update_plan')
			continue;
		const list = parse(call.input.todos ?? call.input.plan ?? call.input.items);
		if (!Array.isArray(list)) continue;
		const todos = list
			.map((t: any) => ({
				content: short(String(t?.content ?? t?.step ?? t?.text ?? ''), 120),
				status: (['pending', 'in_progress', 'completed', 'cancelled'].includes(t?.status)
					? t.status
					: t?.status === 'done'
						? 'completed'
						: 'pending') as TodoItem['status']
			}))
			.filter((t) => t.content);
		if (todos.length) return todos;
	}
	return null;
}

/** Waiting for a permission (OpenCode posts it as a "⏸ Waiting for permission" line). */
export function waitingForPermission(output: any[] | null | undefined): boolean {
	const items = output || [];
	for (let i = items.length - 1; i >= 0; i--) {
		const item = items[i];
		if (item?.type === 'function_call') {
			return String(item.status || '') === 'pending';
		}
		if (item?.type === 'message') {
			const text = (item.content || []).map((c: any) => c?.text || '').join('');
			if (/⏸ Waiting for permission/.test(text)) return true;
			if (text.trim()) return false;
		}
	}
	return false;
}
