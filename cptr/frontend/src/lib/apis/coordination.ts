/**
 * Multi-agent coordination API: tasks, conflicts, messages, integrations, PRs.
 */
import { fetchJSON, jsonBody } from '$lib/apis';

export type TaskStatus =
	| 'queued'
	| 'running'
	| 'blocked'
	| 'paused'
	| 'reviewing'
	| 'integration_ready'
	| 'completed'
	| 'failed'
	| 'ended';

export type ConflictLevel = 'info' | 'potential' | 'confirmed' | 'blocking';

export interface TaskBlocker {
	kind: string;
	detail?: string;
	since?: number;
	conflict_id?: string;
	run_id?: string;
	tasks?: string[];
}

export interface TaskValidation {
	status: 'passed' | 'failed' | 'no_tests' | string;
	scope?: string | null;
	command?: string;
	exit_code?: number | null;
	passed?: boolean;
	timed_out?: boolean;
	duration_ms?: number;
	output_tail?: string;
	head?: string;
	dirty?: boolean;
	at?: number;
}

export interface CoordTask {
	id: string;
	repo_key: string;
	repo_root: string;
	/** This folder, resolved; compare with each task's meta.origin_root. */
	root?: string;
	title: string;
	description: string | null;
	status: TaskStatus;
	chat_id: string | null;
	agent: string | null;
	agent_session_id: string | null;
	branch: string | null;
	base_ref: string | null;
	base_sha: string | null;
	worktree_path: string | null;
	pr_number: number | null;
	pr_url: string | null;
	pr_state: string | null;
	files: string[];
	validation: {
		isolated?: TaskValidation;
		integration?: { run_id: string; passed: boolean; head: string };
	} | null;
	blockers: TaskBlocker[];
	/** Its chat was deleted (list endpoint only). */
	chat_missing?: boolean;
	meta: {
		base_branch?: string;
		milestones?: Record<string, { at: number; [k: string]: unknown }>;
		observed_files?: string[];
		status_reason?: string;
		start?: { prompt: string };
		start_prompt?: string;
		/** Set when the agent's turn ended and it is waiting for the user. */
		waiting_for_user?: number;
		[k: string]: unknown;
	};
	created_at: number;
	updated_at: number;
}

export interface CoordConflict {
	id: string;
	task_a: string;
	task_b: string | null;
	level: ConflictLevel;
	kind: string;
	evidence: {
		files?: string[];
		symbols?: Record<string, string[]>;
		packages?: Record<string, string>[];
		[k: string]: unknown;
	};
	status: 'open' | 'acknowledged' | 'resolved';
	created_at: number;
	updated_at: number;
}

export interface CoordMessage {
	id: string;
	from_task: string | null;
	to_task: string | null;
	kind: string;
	body: string;
	payload: { files?: string[]; [k: string]: unknown };
	delivered_at: number | null;
	created_at: number;
}

export interface IntegrationStep {
	task_id: string;
	result: 'merged' | 'conflict' | 'skipped' | 'excluded';
	commit?: string;
	files?: string[];
	with?: string[];
	reasons?: string[];
	note?: string;
}

export interface CoordIntegration {
	id: string;
	status: 'running' | 'passed' | 'failed' | 'interrupted';
	base_branch: string | null;
	branch: string | null;
	worktree_path: string | null;
	task_order: string[];
	steps: IntegrationStep[];
	tests: (TaskValidation & { attributed_to?: string | null }) | null;
	summary: string | null;
	created_at: number;
	updated_at: number;
}

export interface TaskListResponse {
	repo_root: string;
	/** This folder, resolved; compare with each task's meta.origin_root. */
	root?: string;
	repo_key: string;
	active_chats: string[];
	tasks: CoordTask[];
	dependencies: { task_id: string; depends_on: string }[];
	limits?: { max_writers: number; max_readers: number };
}

const q = (root: string) => `root=${encodeURIComponent(root)}`;

export interface CoordLimits {
	max_writers: number;
	max_readers: number;
	/** Minutes before an agent waiting on its own background work is checked on (0 = never). */
	checkin_minutes: number;
}

export const getCoordLimits = () => fetchJSON<CoordLimits>('/api/coordination/limits');

export const setCoordLimits = (body: { max_writers?: number; checkin_minutes?: number }) =>
	fetchJSON<CoordLimits>('/api/coordination/limits', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});

export const getCoordTasks = (root: string) =>
	fetchJSON<TaskListResponse>(`/api/coordination/tasks?${q(root)}`);

export const getCoordConflicts = (root: string, includeResolved = false) =>
	fetchJSON<{ conflicts: CoordConflict[] }>(
		`/api/coordination/conflicts?${q(root)}&include_resolved=${includeResolved}`
	);

export const getCoordMessages = (root: string, taskId?: string) =>
	fetchJSON<{ messages: CoordMessage[] }>(
		`/api/coordination/messages?${q(root)}${taskId ? `&task_id=${encodeURIComponent(taskId)}` : ''}`
	);

export const getCoordIntegrations = (root: string) =>
	fetchJSON<{ integrations: CoordIntegration[] }>(`/api/coordination/integrations?${q(root)}`);

export interface TaskUsage {
	tokens: number;
	cost_usd: number;
	duration_ms: number;
	tool_calls: number;
	turns: number;
	files: number;
}

export const getCoordUsage = (root: string) =>
	fetchJSON<{ tasks: Record<string, TaskUsage>; totals: TaskUsage }>(
		`/api/coordination/usage?${q(root)}`
	);

export interface RegisterTaskBody {
	root: string;
	title: string;
	description?: string;
	branch?: string;
	/** Branch the task starts from (and integrates into). Default: the repo's HEAD. */
	base_ref?: string;
	depends_on?: string[];
	files?: string[];
	isolate?: boolean;
	create_chat?: boolean;
	start_prompt?: string;
	model_id?: string;
	test_command?: string;
	/** False keeps a task with instructions as a Draft until Start is clicked. */
	start_now?: boolean;
}

export const startCoordTask = (id: string, body: { prompt?: string; model_id?: string } = {}) =>
	fetchJSON<CoordTask>(`/api/coordination/tasks/${id}/start`, jsonBody(body));

export const registerCoordTask = (body: RegisterTaskBody) =>
	fetchJSON<{ task: CoordTask; reused: boolean; worktree: { error?: string } | null }>(
		'/api/coordination/tasks',
		jsonBody(body)
	);

const taskPost = <T = unknown>(id: string, action: string, body: unknown = {}) =>
	fetchJSON<T>(`/api/coordination/tasks/${encodeURIComponent(id)}/${action}`, jsonBody(body));

export const setCoordTaskStatus = (id: string, status: TaskStatus, reason?: string) =>
	taskPost<CoordTask>(id, 'status', { status, reason });
export const pauseCoordTask = (id: string) => taskPost(id, 'pause');
export const resumeCoordTask = (id: string) => taskPost<CoordTask>(id, 'resume');
export const validateCoordTask = (id: string) => taskPost<TaskValidation>(id, 'validate');
export const requestCoordCorrection = (id: string, note?: string) =>
	taskPost(id, 'correction', { note });
export const publishCoordPr = (id: string, draft = false) =>
	taskPost<{ pr: { number: number; url: string; state: string }; created: boolean }>(id, 'pr', {
		confirm: true,
		draft
	});
export const endCoordTask = (id: string) => taskPost(id, 'end');
export const deleteCoordTask = (id: string, removeWorktree: boolean) =>
	fetchJSON<{ ok: boolean; branch_kept: string | null; worktree_removed: boolean }>(
		`/api/coordination/tasks/${encodeURIComponent(id)}`,
		{ ...jsonBody({ confirm: true, remove_worktree: removeWorktree }), method: 'DELETE' }
	);
export const removeCoordWorktree = (id: string) =>
	taskPost(id, 'worktree/remove', { confirm: true });
export const sendCoordMessage = (id: string, body: string) =>
	taskPost(id, 'messages', { kind: 'note', body });
export const addCoordDependency = (id: string, dependsOn: string) =>
	taskPost(id, 'dependencies', { depends_on: dependsOn });
export const removeCoordDependency = (id: string, dependsOn: string) =>
	fetchJSON<{ removed: boolean }>(
		`/api/coordination/tasks/${encodeURIComponent(id)}/dependencies/${encodeURIComponent(dependsOn)}`,
		{ method: 'DELETE' }
	);

export const getCoordTask = (id: string) =>
	fetchJSON<{
		task: CoordTask;
		worktree: { exists: boolean; dirty?: boolean; branch?: string } | null;
	}>(`/api/coordination/tasks/${encodeURIComponent(id)}`);

export const getCoordTaskReadiness = (id: string) =>
	fetchJSON<{ ready: boolean; reasons: string[] }>(
		`/api/coordination/tasks/${encodeURIComponent(id)}/readiness`
	);

export const getCoordTaskDiff = (id: string) =>
	fetchJSON<{ files: any[]; untracked: string[]; merge_base: string; head: string }>(
		`/api/coordination/tasks/${encodeURIComponent(id)}/diff`
	);

export const setCoordConflictStatus = (id: string, status: 'acknowledged' | 'resolved' | 'open') =>
	fetchJSON(`/api/coordination/conflicts/${encodeURIComponent(id)}/status`, jsonBody({ status }));

export const requestCoordIntegration = (root: string, taskIds?: string[]) =>
	fetchJSON<CoordIntegration>(
		'/api/coordination/integrations',
		jsonBody({ root, task_ids: taskIds ?? null })
	);

export const scanCoordination = (root: string) =>
	fetchJSON(`/api/coordination/scan?${q(root)}`, { method: 'POST' });
