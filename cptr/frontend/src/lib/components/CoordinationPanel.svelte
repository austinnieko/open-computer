<script lang="ts">
	/**
	 * Multi-agent coordination dashboard: tasks, worktrees, dependencies,
	 * conflicts, messages, integration runs and PRs for the current repository.
	 * Shows only real runtime state (statuses, files, test results), never
	 * estimated progress.
	 */
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { toast } from 'svelte-sonner';
	import { tooltip } from '$lib/tooltip';
	import Icon from './Icon.svelte';
	import Modal from './Modal.svelte';
	import Spinner from './common/Spinner.svelte';
	import ModelSelector from './common/ModelSelector.svelte';
	import DiffHunkList from './DiffHunkList.svelte';
	import { addWorkspace, openChatTab, workspaceList } from '$lib/stores';
	import { requestChoice, requestConfirm } from '$lib/stores/confirm';
	import { chatModels, defaultModel } from '$lib/stores/chat';
	import { getGitBranches } from '$lib/apis/git';
	import {
		addCoordDependency,
		removeCoordDependency,
		deleteCoordTask,
		markCoordTaskDone,
		endCoordTask,
		getCoordTaskDiff,
		getCoordTaskReadiness,
		pauseCoordTask,
		startCoordTask,
		publishCoordPr,
		registerCoordTask,
		removeCoordWorktree,
		requestCoordCorrection,
		requestCoordIntegration,
		resumeCoordTask,
		scanCoordination,
		sendCoordMessage,
		setCoordConflictStatus,
		setCoordTaskStatus,
		validateCoordTask,
		type CoordConflict,
		type CoordTask,
		type TaskStatus
	} from '$lib/apis/coordination';
	import {
		lifecycleOf,
		checkoutOf,
		type TaskLifecycle,
		coordinationFocus,
		coordinationOf,
		displayStatus,
		levelRank,
		refresh,
		trackCoordination,
		worstConflict,
		compactNumber,
		compactDuration
	} from '$lib/stores/coordination.svelte';
	import type { DiffFile } from '$lib/utils/diff';

	interface Props {
		workspace: string;
	}
	let { workspace }: Props = $props();

	// The prop is re-read whenever the workspace store emits (e.g. on every pane
	// click); $derived memoises the string so tracking only restarts on a real change.
	const root = $derived(workspace);

	$effect(() => {
		const release = trackCoordination(root);
		return release;
	});

	const coord = $derived(coordinationOf(root));
	let selectedId = $state<string | null>(null);
	let showDone = $state(false);
	const HOW_KEY = 'coordination.howItWorks.hidden';
	let showHow = $state(readHowPref());
	function readHowPref(): boolean {
		try {
			return localStorage.getItem(HOW_KEY) !== '1';
		} catch {
			return true;
		}
	}
	function setShowHow(value: boolean) {
		showHow = value;
		try {
			if (value) localStorage.removeItem(HOW_KEY);
			else localStorage.setItem(HOW_KEY, '1');
		} catch {
			// storage unavailable: keep the choice for this session only
		}
	}
	let busy = $state<string | null>(null);
	let showNewTask = $state(false);

	// Which repository these tasks belong to. Tasks are shared by every folder of
	// one repository (its main checkout, its worktrees and any folder inside it).
	const norm = (p: string) => p.replace(/\/+$/, '');
	const repoName = $derived(coord.repoRoot ? norm(coord.repoRoot).split('/').pop() : '');
	const sharedRepo = $derived(!!coord.repoRoot && norm(coord.repoRoot) !== norm(root));

	// Tasks remember the folder they were started from (meta.origin_root). When a
	// repository's tasks come from more than one folder, the list shows this
	// folder's tasks by default and can switch to all of them.
	const originOf = (task: CoordTask) =>
		norm(String(task.meta?.origin_root ?? coord.repoRoot ?? ''));
	const folderName = (path: string) => path.split('/').pop() || path;
	const viewRoot = $derived(norm(coord.viewRoot || root));
	const origins = $derived(new Set(coord.tasks.map(originOf)));
	const mixedFolders = $derived(origins.size > 1 || (origins.size === 1 && !origins.has(viewRoot)));
	let folderScope = $state<'here' | 'all'>('here');
	const hereCount = $derived(coord.tasks.filter((task) => originOf(task) === viewRoot).length);
	const listedTasks = $derived(
		mixedFolders && folderScope === 'here'
			? coord.tasks.filter((task) => originOf(task) === viewRoot)
			: coord.tasks
	);

	// New task can target any open workspace (repo); this one by default. The
	// task still gets its own branch and worktree in the chosen repo.
	let taskRepo = $state('');
	const taskRoot = $derived(taskRepo || root);
	const otherRepo = $derived(norm(taskRoot) !== norm(root));
	const repoOptions = $derived.by(() => {
		const seen = new Set<string>();
		const list: { path: string; name: string }[] = [];
		for (const w of [{ path: root, name: '' }, ...$workspaceList]) {
			const key = norm(w.path);
			if (!w.path || seen.has(key)) continue;
			seen.add(key);
			list.push({ path: w.path, name: w.name || w.path.split('/').pop() || w.path });
		}
		return list;
	});

	// "Start from" branches for New task: the chosen repo's local branches.
	// Task and integration branches are left out. The default is the repo's
	// main branch (main, else master), not whatever the checkout is on.
	let baseOptions = $state<string[]>([]);
	$effect(() => {
		if (!showNewTask) return;
		const path = taskRoot;
		getGitBranches(path)
			.then((data: any) => {
				if (path !== taskRoot) return;
				const local: string[] = (data?.local ?? []).filter(
					(b: string) => !b.startsWith('task/') && !b.startsWith('coord/')
				);
				const current: string = data?.current ?? '';
				baseOptions = current && !local.includes(current) ? [current, ...local] : local;
				// No local main/master but the remote has one: offer it; the server
				// creates the local tracking branch when the task starts.
				const remote: string[] = data?.remote ?? [];
				for (const b of ['main', 'master']) {
					if (!baseOptions.includes(b) && remote.includes(`origin/${b}`)) {
						baseOptions = [b, ...baseOptions];
						break;
					}
				}
				const mainBranch = ['main', 'master'].find((b) => baseOptions.includes(b));
				if (!form.base || !baseOptions.includes(form.base))
					form.base = mainBranch || current || baseOptions[0] || '';
			})
			.catch(() => {
				baseOptions = [];
				form.base = '';
			});
	});

	function chooseTaskRepo(path: string) {
		taskRepo = norm(path) === norm(root) ? '' : path;
		form.base = '';
		form.dependsOn = [];
	}
	let diffFor = $state<string | null>(null);
	let diffFiles = $state<DiffFile[]>([]);
	// Large diffs (hundreds of files) froze the page when every file rendered at
	// once: list the files and render a file's lines only when it is opened.
	const DIFF_AUTO_OPEN_LINES = 400;
	let openDiffFiles = $state<Set<string>>(new Set());
	function fileStats(file: DiffFile): { add: number; del: number; lines: number } {
		let add = 0;
		let del = 0;
		let lines = 0;
		for (const hunk of file.hunks) {
			for (const line of hunk.lines) {
				lines += 1;
				if (line.type === 'added') add += 1;
				else if (line.type === 'removed') del += 1;
			}
		}
		return { add, del, lines };
	}
	function toggleDiffFile(path: string) {
		const next = new Set(openDiffFiles);
		if (next.has(path)) next.delete(path);
		else next.add(path);
		openDiffFiles = next;
	}
	let diffUntracked = $state<string[]>([]);
	let diffLoading = $state(false);
	let readiness = $state<{ ready: boolean; reasons: string[] } | null>(null);
	let note = $state('');

	// A task chip in a chat asked us to show this task.
	$effect(() =>
		coordinationFocus.subscribe((id) => {
			if (!id) return;
			select(id);
			coordinationFocus.set(null);
		})
	);

	const byId = $derived(new Map(coord.tasks.map((task) => [task.id, task])));
	const selected = $derived(selectedId ? (byId.get(selectedId) ?? null) : null);

	// Lifecycle: every task is read as a stage + activity + one main action.
	const lifeCtx = $derived({
		titles: new Map(coord.tasks.map((task) => [task.id, task.title])),
		runningWriters: coord.tasks.filter((task) => task.status === 'running' && task.worktree_path)
			.length,
		maxWriters: coord.maxWriters
	});
	const goneIds = (task: CoordTask) =>
		coord.dependencies
			.filter((d) => d.task_id === task.id)
			.map((d) => d.depends_on)
			.filter((id) =>
				['ended', 'failed'].includes(coord.tasks.find((x) => x.id === id)?.status ?? '')
			);
	const lives = $derived(
		new Map(
			coord.tasks.map((task) => [
				task.id,
				lifecycleOf(task, { ...lifeCtx, goneDeps: goneIds(task) })
			])
		)
	);
	function life(task: CoordTask): TaskLifecycle {
		return lives.get(task.id) ?? lifecycleOf(task, { ...lifeCtx, goneDeps: goneIds(task) });
	}
	function lifeLabel(l: TaskLifecycle): string {
		return l.stage === 'active' && l.activity
			? $t(`coordination.activity.${l.activity}`)
			: $t(`coordination.stage.${l.stage}`);
	}
	function lifeDetail(l: TaskLifecycle): string {
		return l.detailKey ? $t(`coordination.detail.${l.detailKey}`, l.detailArgs ?? {}) : '';
	}
	const LIFE_CLASS: Record<string, string> = {
		working: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300',
		in_line: 'bg-gray-100 text-gray-600 dark:bg-white/8 dark:text-gray-300',
		background: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300',
		needs_you: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
		blocked: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300',
		stopped: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400',
		draft:
			'border border-dashed border-gray-300 text-gray-500 dark:border-white/15 dark:text-gray-400',
		review: 'bg-violet-50 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300',
		done: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400',
		ended: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400'
	};
	function lifeClass(l: TaskLifecycle): string {
		return LIFE_CLASS[l.stage === 'active' && l.activity ? l.activity : l.stage] ?? '';
	}
	const ACTION_ICON: Record<string, string> = {
		start: 'play',
		stop: 'stop',
		reply: 'chat-bubble',
		review: 'git-diff',
		integrate: 'git-branch',
		resume: 'play',
		retry: 'refresh'
	};
	async function primary(task: CoordTask, l: TaskLifecycle) {
		switch (l.action) {
			case 'start':
				return run(`start:${task.id}`, () => startCoordTask(task.id), $t('coordination.started'));
			case 'stop':
				return run(`pause:${task.id}`, () => pauseCoordTask(task.id));
			case 'reply':
				if (task.chat_id) openTaskChat(task);
				return;
			case 'review':
				if (diffFor !== task.id) await loadDiff(task);
				return;
			case 'integrate':
				return integrate();
			case 'resume':
			case 'retry':
				return run(`resume:${task.id}`, () => resumeCoordTask(task.id));
			case 'delete':
				return deleteTask(task);
		}
	}

	// Stages, in order; Done and Ended share the collapsible last group.
	const STAGES = ['active', 'draft', 'review', 'done'] as const;
	const ACTIVITY_ORDER: Record<string, number> = {
		needs_you: 0,
		blocked: 1,
		working: 2,
		background: 2,
		in_line: 3,
		stopped: 4
	};
	const grouped = $derived(
		STAGES.map((key) => ({
			key,
			tasks: listedTasks
				.filter((task) => {
					const stage = life(task).stage;
					return key === 'done' ? stage === 'done' || stage === 'ended' : stage === key;
				})
				.sort(
					(a, b) =>
						(ACTIVITY_ORDER[life(a).activity ?? ''] ?? 9) -
						(ACTIVITY_ORDER[life(b).activity ?? ''] ?? 9)
				)
		})).filter((group) => group.tasks.length > 0)
	);

	// ── Clear finished ─────────────────────────────────────────
	// Done and abandoned tasks in this view, except ones a task that hasn't started
	// yet depends on: it merges their work in when it starts, so they must stay.
	const finishedTasks = $derived(grouped.find((g) => g.key === 'done')?.tasks ?? []);
	const neededBy = (task: CoordTask) =>
		coord.dependencies
			.filter((d) => d.depends_on === task.id)
			.map((d) => coord.tasks.find((x) => x.id === d.task_id))
			.filter(
				(x): x is CoordTask =>
					!!x &&
					!['completed', 'ended'].includes(x.status) &&
					// Once started it has merged its dependencies in (recorded once).
					x.meta?.dependency_merges == null
			);
	const clearable = $derived(finishedTasks.filter((task) => !neededBy(task).length));
	let clearing = $state(false);

	async function clearFinished() {
		const tasks = clearable;
		if (!tasks.length) return;
		const kept = finishedTasks.length - tasks.length;
		const withFolders = tasks.filter((x) => x.worktree_path).length;
		const answer = await requestChoice({
			title: $t('coordination.clearTitle', { count: tasks.length }),
			message:
				$t('coordination.clearMessage', { count: tasks.length }) +
				(kept ? ' ' + $t('coordination.clearKept', { count: kept }) : ''),
			confirmLabel: $t('coordination.clearKeepFolders'),
			...(withFolders
				? {
						altLabel: $t('coordination.clearDeleteFolders', { count: withFolders }),
						altDanger: true
					}
				: { altLabel: '' })
		});
		if (answer === 'cancel') return;
		const removeFolders = answer === 'alt';
		clearing = true;
		let cleared = 0;
		const folderKept: string[] = [];
		const failed: string[] = [];
		for (const task of tasks) {
			try {
				await deleteCoordTask(task.id, removeFolders && !!task.worktree_path);
				cleared += 1;
			} catch (error: any) {
				if (removeFolders && task.worktree_path) {
					// Usually uncommitted changes in its folder: clear the task, keep the folder.
					try {
						await deleteCoordTask(task.id, false);
						cleared += 1;
						folderKept.push(task.title);
						continue;
					} catch {
						// fall through
					}
				}
				failed.push(`${task.title}: ${error?.message ?? error}`);
			}
		}
		clearing = false;
		if (selectedId && tasks.some((x) => x.id === selectedId)) selectedId = null;
		if (cleared) toast.success($t('coordination.cleared', { count: cleared }));
		if (folderKept.length)
			toast.message($t('coordination.clearFolderKept', { tasks: folderKept.join(', ') }));
		if (failed.length) toast.error(failed.join('\n'));
		await refresh(root);
	}

	const openConflicts = $derived(coord.conflicts.filter((c) => c.status !== 'resolved'));
	const counts = $derived({
		running: coord.tasks.filter((task) => task.status === 'running').length,
		active: coord.activeChats.size,
		blocked: coord.tasks.filter((task) => task.status === 'blocked').length,
		conflicts: openConflicts.filter((c) => levelRank(c.level) >= 1).length,
		ready: coord.tasks.filter((task) => task.status === 'reviewing').length
	});

	const STATUS_CLASS: Record<string, string> = {
		queued: 'bg-gray-100 text-gray-600 dark:bg-white/8 dark:text-gray-300',
		awaiting_input: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300',
		waiting_slot: 'bg-gray-100 text-gray-600 dark:bg-white/8 dark:text-gray-300',
		waiting_for_user: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300',
		running: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300',
		blocked: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300',
		paused: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
		reviewing: 'bg-violet-50 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300',
		integration_ready:
			'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
		completed: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400',
		failed: 'bg-red-100 text-red-800 dark:bg-red-500/25 dark:text-red-200',
		ended: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400',
		chat_deleted: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400'
	};
	const LEVEL_CLASS: Record<string, string> = {
		info: 'text-gray-500 dark:text-gray-400',
		potential: 'text-amber-600 dark:text-amber-400',
		confirmed: 'text-orange-600 dark:text-orange-400',
		blocking: 'text-red-600 dark:text-red-400'
	};

	function statusLabel(status: string): string {
		return $t(`coordination.status.${status}`);
	}

	function titleOf(id: string | null): string {
		if (!id) return $t('coordination.baseBranch');
		return byId.get(id)?.title ?? id;
	}

	// Re-render relative times; cheap and local (no network).
	let now = $state(Date.now());
	$effect(() => {
		const timer = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(timer);
	});

	function relTime(ms: number | null | undefined): string {
		if (!ms) return '';
		const seconds = Math.max(0, Math.round((now - ms) / 1000));
		if (seconds < 60) return $t('coordination.secondsAgo', { count: seconds });
		if (seconds < 3600) return $t('coordination.minutesAgo', { count: Math.round(seconds / 60) });
		if (seconds < 86400) return $t('coordination.hoursAgo', { count: Math.round(seconds / 3600) });
		return new Date(ms).toLocaleDateString();
	}

	function dependsOn(id: string): CoordTask[] {
		return coord.dependencies
			.filter((d) => d.task_id === id)
			.map((d) => byId.get(d.depends_on))
			.filter((task): task is CoordTask => !!task);
	}

	function requiredBy(id: string): CoordTask[] {
		return coord.dependencies
			.filter((d) => d.depends_on === id)
			.map((d) => byId.get(d.task_id))
			.filter((task): task is CoordTask => !!task);
	}

	function conflictsOf(id: string): CoordConflict[] {
		return openConflicts
			.filter((c) => c.task_a === id || c.task_b === id)
			.sort((a, b) => levelRank(b.level) - levelRank(a.level));
	}

	function changedFiles(task: CoordTask): string[] {
		return (task.meta.observed_files as string[] | undefined) ?? task.files ?? [];
	}

	async function run(key: string, fn: () => Promise<unknown>, success?: string) {
		busy = key;
		try {
			await fn();
			if (success) toast.success(success);
		} catch (error: any) {
			toast.error(error?.message ?? String(error));
		} finally {
			busy = null;
			await refresh(root);
		}
	}

	function select(id: string) {
		selectedId = id;
		diffFor = null;
		readiness = null;
		note = '';
	}

	async function loadDiff(task: CoordTask) {
		if (diffFor === task.id) {
			diffFor = null;
			return;
		}
		diffFor = task.id;
		diffLoading = true;
		try {
			const result = await getCoordTaskDiff(task.id);
			diffFiles = result.files ?? [];
			// Open everything only when the whole diff is small.
			const total = diffFiles.reduce((n, f) => n + fileStats(f).lines, 0);
			openDiffFiles = new Set(total <= DIFF_AUTO_OPEN_LINES ? diffFiles.map((f) => f.path) : []);
			diffUntracked = result.untracked ?? [];
		} catch (error: any) {
			toast.error(error?.message ?? String(error));
			diffFor = null;
		} finally {
			diffLoading = false;
		}
	}

	async function checkReadiness(task: CoordTask) {
		readiness = await getCoordTaskReadiness(task.id).catch(() => null);
	}

	/** Open a task's chat in the folder the task started from, so it stays listed
	 * there in the sidebar instead of under whichever folder is open now. */
	function openTaskChat(task: CoordTask) {
		if (!task.chat_id) return;
		const origin = originOf(task);
		if (!origin || origin === viewRoot) return openChatTab(task.chat_id);
		const ws = $workspaceList.find((w) => norm(w.path) === origin);
		if (!ws) addWorkspace(origin);
		goto(
			`/?workspace=${encodeURIComponent(ws?.path ?? origin)}&chatId=${encodeURIComponent(task.chat_id)}`
		);
	}

	async function openWorktree(task: CoordTask) {
		if (!task.worktree_path) return;
		addWorkspace(task.worktree_path);
		await goto(`/?workspace=${encodeURIComponent(task.worktree_path)}`);
	}

	async function publish(task: CoordTask) {
		const ok = await requestConfirm({
			title: $t('coordination.publishTitle'),
			message: $t('coordination.publishMessage', { branch: task.branch ?? '' }),
			confirmLabel: $t('coordination.publish')
		});
		if (ok) await run(`pr:${task.id}`, () => publishCoordPr(task.id), $t('coordination.published'));
	}

	async function removeWorktree(task: CoordTask) {
		const ok = await requestConfirm({
			title: $t('coordination.removeWorktreeTitle'),
			message: $t('coordination.removeWorktreeMessage', { path: task.worktree_path ?? '' }),
			confirmLabel: $t('coordination.removeWorktree')
		});
		if (ok) await run(`rm:${task.id}`, () => removeCoordWorktree(task.id));
	}

	/** Tasks still waiting to build on this one. */
	const dependentsOf = (task: CoordTask) =>
		coord.dependencies
			.filter((d) => d.depends_on === task.id)
			.map((d) => coord.tasks.find((x) => x.id === d.task_id))
			.filter((x): x is CoordTask => !!x && !['completed', 'ended'].includes(x.status));

	/** Close a task: mark it done (the usual case) or abandon it. */
	async function endTask(task: CoordTask) {
		const waiting = dependentsOf(task);
		const answer = await requestChoice({
			title: $t('coordination.closeTitle', { title: task.title }),
			message: waiting.length
				? $t('coordination.closeMessageDependents', {
						count: waiting.length,
						tasks: waiting.map((x) => x.title).join(', ')
					})
				: $t('coordination.closeMessage'),
			confirmLabel: $t('coordination.markDone'),
			altLabel: $t('coordination.abandon'),
			altDanger: true
		});
		if (answer === 'confirm')
			await run(`done:${task.id}`, () => markCoordTaskDone(task.id), $t('coordination.markedDone'));
		else if (answer === 'alt')
			await run(`end:${task.id}`, () => endCoordTask(task.id), $t('coordination.ended'));
	}

	async function markDone(task: CoordTask) {
		const ok = await requestConfirm({
			title: $t('coordination.markDoneTitle'),
			message: $t('coordination.markDoneMessage', { title: task.title }),
			confirmLabel: $t('coordination.markDone')
		});
		if (ok)
			await run(`done:${task.id}`, () => markCoordTaskDone(task.id), $t('coordination.markedDone'));
	}
	/** Dependencies that were ended or failed, which hold this task back. */
	const goneDeps = (task: CoordTask) =>
		(task.blockers ?? [])
			.filter((b) => b.kind === 'dependency_failed' || b.detail === 'dependency_failed')
			.flatMap((b) => b.tasks ?? [])

			.concat(goneIds(task))
			.filter((id, i, all) => all.indexOf(id) === i)
			.map((id) => coord.tasks.find((x) => x.id === id))
			.filter((x): x is CoordTask => !!x);

	async function deleteTask(task: CoordTask) {
		const ok = await requestConfirm({
			title: $t('coordination.deleteTitle'),
			message: $t('coordination.deleteMessage', { branch: task.branch ?? '' }),
			confirmLabel: $t('coordination.delete')
		});
		if (!ok) return;
		const removeFolder = task.worktree_path
			? await requestConfirm({
					title: $t('coordination.deleteFolderTitle'),
					message: $t('coordination.deleteFolderMessage', { path: task.worktree_path }),
					confirmLabel: $t('coordination.deleteFolder'),
					cancelLabel: $t('coordination.keepFolder')
				})
			: false;
		await run(
			`delete:${task.id}`,
			async () => {
				await deleteCoordTask(task.id, removeFolder);
				if (selectedId === task.id) selectedId = null;
			},
			$t('coordination.deleted')
		);
	}

	async function integrate() {
		const ok = await requestConfirm({
			title: $t('coordination.integrateTitle'),
			message: $t('coordination.integrateMessage'),
			confirmLabel: $t('coordination.integrate')
		});
		if (!ok) return;
		await run('integrate', async () => {
			const result = await requestCoordIntegration(root);
			if (result.status === 'running') toast.success($t('coordination.integrationStarted'));
			else toast.error(result.summary ?? $t('coordination.run.failed'));
		});
	}

	// ── New task form ────────────────────────────────
	// Same rule as a new chat: the configured default model, else the first one.
	function initialModel(): string | null {
		return $defaultModel || $chatModels[0]?.id || null;
	}

	let form = $state({
		title: '',
		description: '',
		startPrompt: '',
		testCommand: '',
		dependsOn: [] as string[],
		model: initialModel(),
		base: '',
		startNow: true,
		planMode: false,
		followEdits: false
	});

	// The model list can load after this panel mounts; fill the picker once it
	// arrives unless the user already picked something.
	$effect(() => {
		if (!form.model) form.model = initialModel();
	});

	function resetForm() {
		form = {
			title: '',
			description: '',
			startPrompt: '',
			testCommand: '',
			dependsOn: [],
			model: initialModel(),
			base: '',
			startNow: true,
			planMode: false,
			followEdits: false
		};
	}

	async function createTask() {
		if (!form.title.trim()) return;
		if (form.startPrompt.trim() && form.startNow && !form.model) {
			toast.error($t('coordination.modelRequired'));
			return;
		}
		await run('create', async () => {
			const targetRoot = taskRoot;
			const result = await registerCoordTask({
				root: targetRoot,
				title: form.title.trim(),
				description: form.description.trim() || undefined,
				depends_on: otherRepo ? [] : form.dependsOn,
				create_chat: true,
				start_prompt: form.startPrompt.trim() || undefined,
				model_id: form.model ?? undefined,
				test_command: form.testCommand.trim() || undefined,
				base_ref: form.base || undefined,
				start_now: form.startNow,
				plan_mode: form.planMode,
				follow_edits: form.followEdits
			});
			if (result.worktree?.error) toast.error(result.worktree.error);
			if (norm(targetRoot) === norm(root)) {
				selectedId = result.task.id;
			} else {
				const name = targetRoot.split('/').pop() || targetRoot;
				toast.success($t('coordination.taskCreatedIn', { repo: name }));
			}
			showNewTask = false;
			taskRepo = '';
			resetForm();
		});
	}
</script>

<div class="coordination flex h-full min-h-0 flex-col text-sm">
	<!-- Header -->
	<div
		class="flex flex-wrap items-center gap-2 border-b border-gray-100 px-3 py-2 dark:border-white/6"
	>
		<div class="flex min-w-0 items-center gap-2">
			<Icon name="list" size={16} class="shrink-0 text-gray-500" />
			<span class="truncate font-medium text-gray-800 dark:text-gray-100">
				{$t('coordination.title')}
			</span>
			{#if repoName}
				<span class="truncate font-mono text-[0.6875rem] text-gray-400" use:tooltip={coord.repoRoot}
					>{repoName}</span
				>
			{/if}
			{#if coord.loading}<Spinner size={12} />{/if}
		</div>
		<div
			class="flex flex-wrap items-center gap-1.5 text-[0.6875rem] text-gray-500 dark:text-gray-400"
		>
			<span use:tooltip={$t('coordination.runningHint')}>
				{$t('coordination.countRunning', { count: counts.running })}
				{#if counts.active}· {$t('coordination.countActive', { count: counts.active })}{/if}
			</span>
			{#if counts.blocked}
				<span class="text-red-600 dark:text-red-400">
					· {$t('coordination.countBlocked', { count: counts.blocked })}
				</span>
			{/if}
			{#if counts.conflicts}
				<span class="text-amber-600 dark:text-amber-400">
					· {$t('coordination.countConflicts', { count: counts.conflicts })}
				</span>
			{/if}
		</div>
		<div class="ml-auto flex items-center gap-1">
			<button
				class="icon-btn"
				use:tooltip={$t('coordination.scan')}
				aria-label={$t('coordination.scan')}
				disabled={busy === 'scan' || !coord.tasks.length}
				onclick={() => run('scan', () => scanCoordination(root))}
			>
				<Icon name="refresh" size={14} />
			</button>
			<button
				class="btn-secondary"
				disabled={busy === 'integrate' || !counts.ready}
				onclick={integrate}
				use:tooltip={$t('coordination.integrateHint')}
			>
				<Icon name="git-branch" size={13} />
				<span class="hidden sm:inline">{$t('coordination.integrate')}</span>
			</button>
			<button class="btn-primary" disabled={coord.notRepo} onclick={() => (showNewTask = true)}>
				<Icon name="plus" size={13} />
				<span class="hidden sm:inline">{$t('coordination.newTask')}</span>
			</button>
		</div>
	</div>

	{#if sharedRepo}
		<div
			class="flex items-start gap-2 border-b border-gray-100 bg-sky-50/60 px-3 py-2 text-xs text-gray-600 dark:border-white/6 dark:bg-sky-500/[0.06] dark:text-gray-300"
		>
			<Icon name="info" size={13} class="mt-0.5 shrink-0 text-sky-600 dark:text-sky-400" />
			<span>{$t('coordination.sharedRepo', { repo: repoName, path: coord.repoRoot })}</span>
		</div>
	{/if}

	{#if !coord.notRepo}
		{#if showHow || (coord.loaded && !coord.tasks.length)}
			<div
				class="how border-b border-gray-100 bg-gray-50/60 px-3 py-2.5 dark:border-white/6 dark:bg-white/[0.02]"
			>
				<div class="mb-1.5 flex items-center gap-2">
					<Icon name="info" size={13} class="text-gray-500" />
					<span class="text-xs font-medium text-gray-700 dark:text-gray-200"
						>{$t('coordination.how.title')}</span
					>
					{#if coord.tasks.length}
						<button class="link ml-auto text-[0.6875rem]" onclick={() => setShowHow(false)}
							>{$t('coordination.how.hide')}</button
						>
					{/if}
				</div>
				<ol class="grid gap-2 text-[0.75rem] text-gray-600 sm:grid-cols-3 dark:text-gray-300">
					<li class="how-step">
						<span class="how-num">1</span>
						<span
							><b>{$t('coordination.how.step1Title')}</b>
							{$t('coordination.how.step1')}</span
						>
					</li>
					<li class="how-step">
						<span class="how-num">2</span>
						<span
							><b>{$t('coordination.how.step2Title')}</b>
							{$t('coordination.how.step2')}</span
						>
					</li>
					<li class="how-step">
						<span class="how-num">3</span>
						<span
							><b>{$t('coordination.how.step3Title')}</b>
							{$t('coordination.how.step3')}</span
						>
					</li>
				</ol>
			</div>
		{/if}
		{#if coord.usageTotals && coord.tasks.length}
			<div
				class="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-gray-100 px-3 py-1.5 text-[0.6875rem] text-gray-500 dark:border-white/6 dark:text-gray-400"
			>
				<span class="font-medium uppercase tracking-wide text-gray-400"
					>{$t('coordination.usage.allTasks')}</span
				>
				<span use:tooltip={$t('coordination.usage.tokensHint')}
					><b class="stat">{compactNumber(coord.usageTotals.tokens)}</b>
					{$t('coordination.usage.tokens')}</span
				>
				<span use:tooltip={$t('coordination.usage.timeHint')}
					><b class="stat">{compactDuration(coord.usageTotals.duration_ms)}</b>
					{$t('coordination.usage.working')}</span
				>
				<span
					><b class="stat">{compactNumber(coord.usageTotals.tool_calls)}</b>
					{$t('coordination.usage.toolCalls')}</span
				>
				<span
					><b class="stat">{compactNumber(coord.usageTotals.files)}</b>
					{$t('coordination.usage.files')}</span
				>
				{#if coord.usageTotals.cost_usd > 0}
					<span><b class="stat">${coord.usageTotals.cost_usd.toFixed(2)}</b></span>
				{/if}
				{#if !showHow}
					<button class="link ml-auto" onclick={() => setShowHow(true)}
						>{$t('coordination.how.show')}</button
					>
				{/if}
			</div>
		{/if}
	{/if}

	{#if coord.notRepo}
		<div class="empty">{$t('coordination.notRepo')}</div>
	{:else if coord.error}
		<div class="empty text-red-600 dark:text-red-400">{coord.error}</div>
	{:else if coord.loaded && !coord.tasks.length}
		<div class="empty">
			<p>{$t('coordination.empty')}</p>
			<button class="btn-primary mt-3" onclick={() => (showNewTask = true)}>
				<Icon name="plus" size={13} />{$t('coordination.newTask')}
			</button>
		</div>
	{:else}
		<div class="grid min-h-0 flex-1 md:grid-cols-[minmax(15rem,20rem)_1fr]">
			<!-- Task list -->
			<div
				class="min-h-0 overflow-y-auto border-gray-100 md:border-r dark:border-white/6 {selected
					? 'hidden md:block'
					: 'block'}"
			>
				{#if mixedFolders}
					<div class="folder-scope" role="tablist">
						<button
							role="tab"
							aria-selected={folderScope === 'here'}
							class:folder-scope-on={folderScope === 'here'}
							onclick={() => (folderScope = 'here')}
							use:tooltip={viewRoot}
						>
							{$t('coordination.scope.here', { folder: folderName(viewRoot) })} · {hereCount}
						</button>
						<button
							role="tab"
							aria-selected={folderScope === 'all'}
							class:folder-scope-on={folderScope === 'all'}
							onclick={() => (folderScope = 'all')}
							use:tooltip={$t('coordination.scope.allHint', { repo: repoName })}
						>
							{$t('coordination.scope.all', { repo: repoName })} · {coord.tasks.length}
						</button>
					</div>
					{#if folderScope === 'here' && !hereCount}
						<p class="px-3 py-2 text-xs text-gray-500">
							{$t('coordination.scope.noneHere', { count: coord.tasks.length })}
						</p>
					{/if}
				{/if}
				{#each grouped as group (group.key)}
					{#if group.key !== 'done' || showDone}
						<div class="section-label">
							{$t(`coordination.group.${group.key}`)} · {group.tasks.length}
						</div>
						{#each group.tasks as task (task.id)}
							{@const worst = worstConflict(openConflicts, task.id)}
							{@const rowLife = life(task)}
							<button
								class="task-row"
								class:task-row-selected={task.id === selectedId}
								onclick={() => select(task.id)}
							>
								<div class="flex min-w-0 items-center gap-1.5">
									{#if task.chat_id && coord.activeChats.has(task.chat_id)}
										<span
											class="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-blue-500"
											use:tooltip={$t('coordination.agentWorking')}
										></span>
									{/if}
									<span class="truncate font-medium text-gray-800 dark:text-gray-100">
										{task.title}
									</span>
									{#if mixedFolders && folderScope === 'all'}
										<span
											class="folder-chip"
											class:folder-chip-here={originOf(task) === viewRoot}
											use:tooltip={$t('coordination.startedIn', { path: originOf(task) })}
										>
											<Icon name="folder" size={10} />{folderName(originOf(task))}
										</span>
									{/if}
									{#if worst && levelRank(worst.level) >= 1}
										<span
											class="ml-auto shrink-0 text-[0.625rem] font-semibold uppercase {LEVEL_CLASS[
												worst.level
											]}"
											use:tooltip={$t(`coordination.kind.${worst.kind}`)}
										>
											{$t(`coordination.level.${worst.level}`)}
										</span>
									{/if}
								</div>
								<div
									class="mt-0.5 flex min-w-0 items-center gap-1.5 text-[0.6875rem] text-gray-500 dark:text-gray-400"
								>
									<span class="pill {lifeClass(rowLife)}">{lifeLabel(rowLife)}</span>
									{#if rowLife.activity === 'needs_you' && rowLife.detailText}
										<span class="truncate text-amber-700 dark:text-amber-300"
											>{rowLife.detailText}</span
										>
									{:else if task.branch}<span class="truncate font-mono">{task.branch}</span>{/if}
									{#if task.pr_number}
										<span class="shrink-0">#{task.pr_number}</span>
									{/if}
									{#if coord.usage[task.id]?.tokens}
										<span
											class="ml-auto shrink-0 tabular-nums text-gray-400"
											use:tooltip={$t('coordination.usage.tokensHint')}
											>{compactNumber(coord.usage[task.id].tokens)}</span
										>
									{/if}
								</div>
							</button>
						{/each}
					{/if}
				{/each}
				{#if grouped.some((g) => g.key === 'done')}
					<div class="flex items-center">
						<button class="section-toggle flex-1" onclick={() => (showDone = !showDone)}>
							{showDone ? $t('coordination.hideDone') : $t('coordination.showDone')}
						</button>
						{#if clearable.length}
							<button
								class="section-toggle touch-target w-auto! shrink-0 text-right hover:text-gray-800 dark:hover:text-gray-200"
								disabled={clearing}
								onclick={clearFinished}
								use:tooltip={$t('coordination.clearHint')}
							>
								{clearing
									? $t('coordination.clearing')
									: $t('coordination.clearFinished', { count: clearable.length })}
							</button>
						{/if}
					</div>
				{/if}

				{#if coord.integrations.length}
					<div class="section-label mt-2">{$t('coordination.integrations')}</div>
					{#each coord.integrations.slice(0, 5) as runItem (runItem.id)}
						<details class="px-3 py-1.5 text-[0.6875rem] text-gray-600 dark:text-gray-300">
							<summary class="cursor-pointer">
								<span
									class={runItem.status === 'passed'
										? 'text-emerald-600 dark:text-emerald-400'
										: runItem.status === 'running'
											? 'text-blue-600 dark:text-blue-400'
											: 'text-red-600 dark:text-red-400'}
								>
									{$t(`coordination.run.${runItem.status}`)}
								</span>
								· {relTime(runItem.created_at)}
							</summary>
							<p class="mt-1 text-gray-500 dark:text-gray-400">{runItem.summary ?? ''}</p>
							{#if runItem.branch}<p class="font-mono">{runItem.branch}</p>{/if}
							<ol class="mt-1 space-y-0.5">
								{#each runItem.steps as step}
									<li>
										<span class="font-medium">{titleOf(step.task_id)}</span>:
										{$t(`coordination.step.${step.result}`)}
										{#if step.files?.length}({step.files.join(', ')}){/if}
										{#if step.reasons?.length}— {step.reasons.join('; ')}{/if}
										{#if step.note}— {step.note}{/if}
									</li>
								{/each}
							</ol>
							{#if runItem.tests}
								<p class="mt-1">
									{$t('coordination.tests')}: {runItem.tests.passed
										? $t('coordination.passed')
										: $t('coordination.failed')}
									{#if runItem.tests.attributed_to}
										— {$t('coordination.firstFailing')}: {titleOf(runItem.tests.attributed_to)}
									{/if}
								</p>
							{/if}
						</details>
					{/each}
				{/if}
			</div>

			<!-- Detail -->
			<div class="min-h-0 overflow-y-auto {selected ? 'block' : 'hidden md:block'}">
				{#if !selected}
					<div class="empty">{$t('coordination.selectTask')}</div>
				{:else}
					{@const task = selected}
					{@const taskLife = life(selected)}
					<div class="space-y-4 p-3 sm:p-4">
						<div>
							<button
								class="mb-2 flex items-center gap-1 text-xs text-gray-500 md:hidden"
								onclick={() => (selectedId = null)}
							>
								<Icon name="chevron-left" size={12} />{$t('coordination.back')}
							</button>
							<div class="flex flex-wrap items-center gap-2">
								<h2 class="min-w-0 text-base font-semibold text-gray-900 dark:text-gray-50">
									{task.title}
								</h2>
								<span class="pill {lifeClass(taskLife)}"
									>{taskLife.stage === 'active' && taskLife.activity
										? `${$t('coordination.stage.active')} · ${lifeLabel(taskLife)}`
										: lifeLabel(taskLife)}</span
								>
								{#if task.chat_id && coord.activeChats.has(task.chat_id)}
									<span
										class="flex items-center gap-1 text-[0.6875rem] text-blue-600 dark:text-blue-400"
									>
										<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500"></span>
										{$t('coordination.agentWorking')}
									</span>
								{/if}
							</div>
							{#if task.description}
								<p class="mt-1 whitespace-pre-wrap text-gray-600 dark:text-gray-300">
									{task.description}
								</p>
							{/if}
						</div>

						{#if !task.chat_missing}
							{@const l = life(task)}
							{@const checkout = checkoutOf(task)}
							<!-- What is happening, and the one thing to do next. -->
							<div class="next-step">
								<div class="min-w-0 flex-1">
									{#if lifeDetail(l)}<p>{lifeDetail(l)}</p>{/if}
									{#if l.detailText}
										<p class="mt-0.5 whitespace-pre-wrap font-medium">{l.detailText}</p>
									{/if}
									{#if !lifeDetail(l) && !l.detailText}
										<p>{$t('coordination.detail.working')}</p>
									{/if}
								</div>
								{#if l.action && l.action !== 'delete'}
									<button
										class={l.action === 'stop' ? 'btn-secondary' : 'btn-primary'}
										disabled={busy === `start:${task.id}` ||
											busy === `pause:${task.id}` ||
											busy === `resume:${task.id}` ||
											busy === 'integrate'}
										onclick={() => primary(task, l)}
									>
										<Icon name={ACTION_ICON[l.action] ?? 'play'} size={13} />{$t(
											`coordination.action.${l.action}`
										)}
									</button>
								{/if}
							</div>
							{#if checkout?.note && !(l.activity === 'needs_you' && l.detailText)}
								<div class="report">
									<span class="label mb-0">{$t('coordination.lastReport')}</span>
									<span
										class="pill {checkout.kind === 'done' ? LIFE_CLASS.review : LIFE_CLASS.in_line}"
										>{$t(`coordination.checkout.${checkout.kind}`)}</span
									>
									<p class="mt-1 whitespace-pre-wrap">{checkout.note}</p>
								</div>
							{/if}
							{#if Array.isArray(task.meta.dependency_merges) && task.meta.dependency_merges.length}
								<div class="report">
									<span class="label mb-0">{$t('coordination.handoff')}</span>
									<ul class="mt-1 space-y-0.5">
										{#each task.meta.dependency_merges as merge}
											<li>
												{$t(`coordination.merge.${merge.result}`, {
													task: titleOf(merge.task),
													branch: merge.branch
												})}
											</li>
										{/each}
									</ul>
								</div>
							{/if}
						{/if}

						{#if task.chat_missing}
							<!-- Its chat was deleted: nothing can run it, so offer removal up front. -->
							<div class="chat-gone">
								<p>{$t('coordination.chatGoneMessage')}</p>
								<button
									class="btn-danger"
									disabled={busy === `delete:${task.id}`}
									onclick={() => deleteTask(task)}
								>
									<Icon name="trash" size={13} />{$t('coordination.deleteTask')}
								</button>
							</div>
						{/if}

						<!-- Actions -->
						<div class="flex flex-wrap gap-1.5">
							{#if task.chat_id && !task.chat_missing}
								<button class="btn-secondary" onclick={() => openTaskChat(task)}>
									<Icon name="chat-bubble" size={13} />{$t('coordination.openChat')}
								</button>
							{/if}
							{#if task.worktree_path}
								<button class="btn-secondary" onclick={() => openWorktree(task)}>
									<Icon name="folder" size={13} />{$t('coordination.openWorktree')}
								</button>
								<button class="btn-secondary" onclick={() => loadDiff(task)}>
									<Icon name="git-diff" size={13} />{diffFor === task.id
										? $t('coordination.hideDiff')
										: $t('coordination.viewDiff')}
								</button>
							{/if}
							{#if ['blocked', 'reviewing'].includes(task.status) && !task.chat_missing}
								<button
									class="btn-secondary"
									disabled={busy === `pause:${task.id}`}
									onclick={() => run(`pause:${task.id}`, () => pauseCoordTask(task.id))}
								>
									<Icon name="stop" size={13} />{$t('coordination.action.stop')}
								</button>
							{/if}
							{#if !['completed', 'ended'].includes(task.status) && !task.chat_missing}
								<button
									class="btn-secondary"
									disabled={busy === `end:${task.id}`}
									onclick={() => endTask(task)}
									use:tooltip={$t('coordination.endHint')}
								>
									<Icon name="xmark" size={13} />{$t('coordination.end')}
								</button>
							{/if}
							{#if task.status === 'ended' && !task.chat_missing}
								<button
									class="btn-secondary"
									disabled={busy === `done:${task.id}`}
									onclick={() => markDone(task)}
									use:tooltip={$t('coordination.markDoneHint')}
								>
									<Icon name="check" size={13} />{task.status === 'ended'
										? $t('coordination.markDoneInstead')
										: $t('coordination.markDone')}
								</button>
							{/if}
							{#each goneDeps(task) as dep (dep.id)}
								<button
									class="btn-secondary"
									disabled={busy === `done:${dep.id}`}
									onclick={() => markDone(dep)}
									use:tooltip={$t('coordination.markDepDoneHint')}
								>
									<Icon name="check" size={13} />{$t('coordination.markDepDone', {
										title: dep.title
									})}
								</button>
							{/each}
							{#if task.status === 'running'}
								<button
									class="btn-secondary"
									onclick={() =>
										run(`status:${task.id}`, () =>
											setCoordTaskStatus(task.id, 'reviewing', 'marked ready by user')
										)}
								>
									<Icon name="check" size={13} />{$t('coordination.markReady')}
								</button>
							{/if}
							{#if task.worktree_path}
								<button
									class="btn-secondary"
									disabled={busy === `validate:${task.id}`}
									onclick={() =>
										run(
											`validate:${task.id}`,
											() => validateCoordTask(task.id),
											$t('coordination.validated')
										)}
								>
									{#if busy === `validate:${task.id}`}<Spinner size={12} />{:else}<Icon
											name="check-square"
											size={13}
										/>{/if}
									{$t('coordination.runTests')}
								</button>
							{/if}
							{#if task.blockers.some((b) => b.kind === 'needs_correction') && task.chat_id}
								<button
									class="btn-secondary"
									onclick={() => run(`fix:${task.id}`, () => requestCoordCorrection(task.id))}
									use:tooltip={$t('coordination.requestFixHint')}
								>
									<Icon name="tools" size={13} />{$t('coordination.requestFix')}
								</button>
							{/if}
							{#if task.status === 'integration_ready' || task.status === 'completed'}
								<button
									class="btn-primary"
									disabled={busy === `pr:${task.id}`}
									onclick={() => publish(task)}
								>
									<Icon name="upload" size={13} />{task.pr_number
										? $t('coordination.updatePr')
										: $t('coordination.publishPr')}
								</button>
							{/if}
						</div>

						{#if diffFor === task.id}
							<div class="rounded border border-gray-100 dark:border-white/6">
								{#if diffLoading}
									<div class="p-4"><Spinner size={14} /></div>
								{:else if !diffFiles.length && !diffUntracked.length}
									<div class="p-3 text-xs text-gray-500">{$t('coordination.noChanges')}</div>
								{:else}
									<div
										class="border-b border-gray-100 px-2 py-1 text-[0.6875rem] text-gray-500 dark:border-white/6"
									>
										{$t('coordination.diffFiles', { count: diffFiles.length })}
									</div>
									<div class="overflow-x-auto font-mono text-[0.6875rem] leading-[1.125rem]">
										{#each diffFiles as file (file.path)}
											{@const stats = fileStats(file)}
											{@const open = openDiffFiles.has(file.path)}
											<button
												type="button"
												class="sticky top-0 flex w-full items-center gap-1.5 border-b border-gray-100 bg-gray-50 px-2 py-1 text-left text-[0.625rem] font-medium text-gray-500 hover:bg-gray-100 dark:border-white/4 dark:bg-white/3 dark:text-gray-400 dark:hover:bg-white/6"
												aria-expanded={open}
												onclick={() => toggleDiffFile(file.path)}
											>
												<span
													class="flex shrink-0"
													style="transform: rotate({open ? '90deg' : '0deg'})"
												>
													<Icon name="chevron-right" size={9} />
												</span>
												<span class="min-w-0 flex-1 truncate">{file.path}</span>
												<span class="shrink-0 text-emerald-600 dark:text-emerald-400"
													>+{stats.add}</span
												>
												<span class="shrink-0 text-red-600 dark:text-red-400">−{stats.del}</span>
											</button>
											{#if open}
												<DiffHunkList hunks={file.hunks} path={file.path} showNumbers />
											{/if}
										{/each}
									</div>
									{#if diffUntracked.length}
										<div
											class="border-t border-gray-100 p-2 text-xs text-gray-500 dark:border-white/6"
										>
											{$t('coordination.untracked')}: {diffUntracked.join(', ')}
										</div>
									{/if}
								{/if}
							</div>
						{/if}

						<!-- Facts -->
						<dl class="facts">
							<dt>{$t('coordination.branch')}</dt>
							<dd class="font-mono">{task.branch ?? '—'}</dd>
							<dt>{$t('coordination.startedFrom')}</dt>
							<dd class="break-all">
								<span class="font-mono">{folderName(originOf(task))}</span>
								<span class="ml-1 text-[0.6875rem] text-gray-400">{originOf(task)}</span>
							</dd>
							<dt>{$t('coordination.base')}</dt>
							<dd class="font-mono">{task.meta.base_branch ?? task.base_ref ?? '—'}</dd>
							<dt>{$t('coordination.worktree')}</dt>
							<dd class="break-all">
								<span class="font-mono">{task.worktree_path ?? $t('coordination.readOnly')}</span>
								{#if task.worktree_path}
									<span class="mt-0.5 block text-[0.6875rem] text-gray-400"
										>{$t('coordination.worktreeHint')}</span
									>
								{/if}
							</dd>
							<dt>{$t('coordination.agent')}</dt>
							<dd>
								{task.agent ?? '—'}
								{#if task.agent_session_id}<span class="font-mono text-gray-400">
										· {task.agent_session_id}</span
									>{/if}
							</dd>
							<dt>{$t('coordination.pr')}</dt>
							<dd>
								{#if task.pr_url}
									<a
										class="text-blue-600 hover:underline dark:text-blue-400"
										href={task.pr_url}
										target="_blank"
										rel="noreferrer"
									>
										#{task.pr_number} · {task.pr_state}
									</a>
								{:else}—{/if}
							</dd>
							<dt>{$t('coordination.updated')}</dt>
							<dd>{relTime(task.updated_at)}</dd>
						</dl>

						{#if coord.usage[task.id]}
							{@const u = coord.usage[task.id]}
							<section>
								<h3 class="section-title">{$t('coordination.usage.title')}</h3>
								<div class="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
									<div class="tile" use:tooltip={$t('coordination.usage.tokensHint')}>
										<span class="tile-value">{compactNumber(u.tokens)}</span>
										<span class="tile-label">{$t('coordination.usage.tokens')}</span>
									</div>
									<div class="tile" use:tooltip={$t('coordination.usage.timeHint')}>
										<span class="tile-value">{compactDuration(u.duration_ms)}</span>
										<span class="tile-label">{$t('coordination.usage.working')}</span>
									</div>
									<div class="tile">
										<span class="tile-value">{compactNumber(u.tool_calls)}</span>
										<span class="tile-label">{$t('coordination.usage.toolCalls')}</span>
									</div>
									<div class="tile">
										<span class="tile-value">{compactNumber(u.files)}</span>
										<span class="tile-label">{$t('coordination.usage.files')}</span>
									</div>
								</div>
								<p class="mt-1 text-[0.6875rem] text-gray-400">
									{$t('coordination.usage.turns', { count: u.turns })}{#if u.cost_usd > 0}
										· ${u.cost_usd.toFixed(2)}{/if}
								</p>
							</section>
						{/if}

						{#if task.blockers.length}
							<section>
								<h3 class="section-title">{$t('coordination.blockers')}</h3>
								<ul class="space-y-1">
									{#each task.blockers as blocker}
										<li
											class="rounded bg-red-50 px-2 py-1 text-xs text-red-700 dark:bg-red-500/10 dark:text-red-300"
										>
											<span class="font-medium">{$t(`coordination.blocker.${blocker.kind}`)}</span>
											{#if blocker.kind === 'waiting_on' && blocker.tasks}
												: {blocker.tasks.map(titleOf).join(', ')}
											{:else if blocker.detail}: {blocker.detail}{/if}
										</li>
									{/each}
								</ul>
							</section>
						{/if}

						<section>
							<h3 class="section-title">{$t('coordination.dependencies')}</h3>
							{#if !dependsOn(task.id).length && !requiredBy(task.id).length}
								<p class="text-xs text-gray-500">{$t('coordination.noDependencies')}</p>
							{/if}
							{#each dependsOn(task.id) as dep}
								<div class="dep-line">
									<button class="dep-row" onclick={() => select(dep.id)}>
										<Icon name="arrow-left" size={12} />
										<span class="truncate">{$t('coordination.dependsOn')} {dep.title}</span>
										<span class="pill ml-auto {STATUS_CLASS[displayStatus(dep)]}"
											>{statusLabel(displayStatus(dep))}</span
										>
									</button>
									<button
										class="dep-remove"
										title={$t('coordination.removeDependency')}
										aria-label={$t('coordination.removeDependency')}
										disabled={busy === `dep:${task.id}`}
										onclick={() =>
											run(`dep:${task.id}`, () => removeCoordDependency(task.id, dep.id))}
									>
										<Icon name="xmark" size={12} />
									</button>
								</div>
							{/each}
							{#each requiredBy(task.id) as dep}
								<div class="dep-line">
									<button class="dep-row" onclick={() => select(dep.id)}>
										<Icon name="arrow-right-from-line" size={12} />
										<span class="truncate">{$t('coordination.requiredBy')} {dep.title}</span>
										<span class="pill ml-auto {STATUS_CLASS[displayStatus(dep)]}"
											>{statusLabel(displayStatus(dep))}</span
										>
									</button>
									<button
										class="dep-remove"
										title={$t('coordination.removeDependency')}
										aria-label={$t('coordination.removeDependency')}
										disabled={busy === `dep:${dep.id}`}
										onclick={() =>
											run(`dep:${dep.id}`, () => removeCoordDependency(dep.id, task.id))}
									>
										<Icon name="xmark" size={12} />
									</button>
								</div>
							{/each}
							{#if dependsOn(task.id).length && task.status === 'blocked'}
								<p class="mt-1 text-xs text-gray-500">{$t('coordination.removeDependencyHint')}</p>
							{/if}
							{#if coord.tasks.filter((o) => o.id !== task.id && !['completed', 'failed', 'ended'].includes(o.status) && !dependsOn(task.id).some((d) => d.id === o.id)).length}
								<select
									class="field mt-1 text-xs"
									value=""
									onchange={(e) => {
										const target = (e.currentTarget as HTMLSelectElement).value;
										if (target) run(`dep:${task.id}`, () => addCoordDependency(task.id, target));
										(e.currentTarget as HTMLSelectElement).value = '';
									}}
								>
									<option value="">{$t('coordination.addDependency')}</option>
									{#each coord.tasks.filter((o) => o.id !== task.id && !['completed', 'failed', 'ended'].includes(o.status) && !dependsOn(task.id).some((d) => d.id === o.id)) as other}
										<option value={other.id}>{other.title}</option>
									{/each}
								</select>
							{/if}
						</section>

						<section>
							<h3 class="section-title">{$t('coordination.conflicts')}</h3>
							{#if !conflictsOf(task.id).length}
								<p class="text-xs text-gray-500">{$t('coordination.noConflicts')}</p>
							{/if}
							<ul class="space-y-1.5">
								{#each conflictsOf(task.id) as conflict (conflict.id)}
									{@const other = conflict.task_a === task.id ? conflict.task_b : conflict.task_a}
									<li
										class="rounded border border-gray-100 px-2 py-1.5 text-xs dark:border-white/6"
									>
										<div class="flex flex-wrap items-center gap-1.5">
											<span class="font-semibold uppercase {LEVEL_CLASS[conflict.level]}">
												{$t(`coordination.level.${conflict.level}`)}
											</span>
											<span class="text-gray-700 dark:text-gray-200">
												{$t(`coordination.kind.${conflict.kind}`)}
											</span>
											<span class="text-gray-500">· {$t('coordination.with')} {titleOf(other)}</span
											>
											{#if conflict.status === 'acknowledged'}
												<span class="text-gray-400">· {$t('coordination.acknowledged')}</span>
											{/if}
											<span class="ml-auto flex gap-1">
												{#if conflict.status === 'open'}
													<button
														class="link"
														onclick={() =>
															run(`ack:${conflict.id}`, () =>
																setCoordConflictStatus(conflict.id, 'acknowledged')
															)}
														use:tooltip={$t('coordination.acknowledgeHint')}
														>{$t('coordination.acknowledge')}</button
													>
												{/if}
												<button
													class="link"
													onclick={() =>
														run(`res:${conflict.id}`, () =>
															setCoordConflictStatus(conflict.id, 'resolved')
														)}
													use:tooltip={$t('coordination.resolveHint')}
													>{$t('coordination.resolve')}</button
												>
											</span>
										</div>
										{#if conflict.evidence.files?.length}
											<div class="mt-0.5 break-all font-mono text-[0.6875rem] text-gray-500">
												{conflict.evidence.files.join(', ')}
											</div>
										{/if}
										{#if conflict.evidence.symbols}
											<div class="mt-0.5 font-mono text-[0.6875rem] text-gray-500">
												{Object.keys(conflict.evidence.symbols).join(', ')}
											</div>
										{/if}
										{#if conflict.evidence.packages}
											<div class="mt-0.5 font-mono text-[0.6875rem] text-gray-500">
												{conflict.evidence.packages.map((p) => p.package).join(', ')}
											</div>
										{/if}
									</li>
								{/each}
							</ul>
						</section>

						<section>
							<h3 class="section-title">{$t('coordination.validation')}</h3>
							{#if task.validation?.isolated}
								{@const v = task.validation.isolated}
								<p class="text-xs">
									<span
										class={v.status === 'passed'
											? 'text-emerald-600 dark:text-emerald-400'
											: v.status === 'failed'
												? 'text-red-600 dark:text-red-400'
												: 'text-gray-500'}
									>
										{$t(`coordination.validationStatus.${v.status}`)}
									</span>
									{#if v.command}<span class="font-mono text-gray-500"> · {v.command}</span>{/if}
									{#if v.duration_ms != null}<span class="text-gray-400">
											· {(v.duration_ms / 1000).toFixed(1)}s</span
										>{/if}
									{#if v.at}<span class="text-gray-400"> · {relTime(v.at)}</span>{/if}
								</p>
								{#if v.status === 'failed' && v.output_tail}
									<pre class="output">{v.output_tail.slice(-3000)}</pre>
								{/if}
							{:else}
								<p class="text-xs text-gray-500">{$t('coordination.notValidated')}</p>
							{/if}
							{#if task.validation?.integration}
								<p class="mt-1 text-xs text-emerald-600 dark:text-emerald-400">
									{$t('coordination.integrationPassed', {
										run: task.validation.integration.run_id
									})}
								</p>
							{/if}
							{#if task.status === 'reviewing'}
								<button class="link mt-1 text-xs" onclick={() => checkReadiness(task)}>
									{$t('coordination.checkReadiness')}
								</button>
								{#if readiness}
									<p class="mt-1 text-xs {readiness.ready ? 'text-emerald-600' : 'text-amber-600'}">
										{readiness.ready ? $t('coordination.ready') : readiness.reasons.join('; ')}
									</p>
								{/if}
							{/if}
						</section>

						{#if changedFiles(task).length}
							<section>
								<h3 class="section-title">
									{$t('coordination.files')} · {changedFiles(task).length}
								</h3>
								<p class="break-all font-mono text-[0.6875rem] text-gray-500">
									{changedFiles(task).slice(0, 40).join(', ')}
								</p>
							</section>
						{/if}

						<section>
							<h3 class="section-title">{$t('coordination.messages')}</h3>
							<ul class="space-y-1">
								{#each coord.messages
									.filter((m) => m.to_task === task.id || m.from_task === task.id)
									.slice(0, 30) as message (message.id)}
									<li class="text-xs">
										<span class="text-gray-400">{relTime(message.created_at)}</span>
										<span class="font-medium text-gray-600 dark:text-gray-300">
											{message.from_task === task.id
												? `→ ${titleOf(message.to_task)}`
												: `← ${message.from_task ? titleOf(message.from_task) : $t('coordination.coordinator')}`}
										</span>
										<span class="text-gray-400">[{message.kind}]</span>
										<span class="text-gray-700 dark:text-gray-200">{message.body}</span>
										{#if message.to_task === task.id && !message.delivered_at}
											<span class="text-amber-600 dark:text-amber-400"
												>· {$t('coordination.pendingDelivery')}</span
											>
										{/if}
									</li>
								{:else}
									<li class="text-xs text-gray-500">{$t('coordination.noMessages')}</li>
								{/each}
							</ul>
							<form
								class="mt-2 flex gap-1.5"
								onsubmit={(e) => {
									e.preventDefault();
									if (!note.trim()) return;
									const body = note.trim();
									note = '';
									run(`note:${task.id}`, () => sendCoordMessage(task.id, body));
								}}
							>
								<input
									class="field flex-1 text-xs"
									bind:value={note}
									placeholder={$t('coordination.notePlaceholder')}
								/>
								<button class="btn-secondary" type="submit" disabled={!note.trim()}>
									{$t('coordination.send')}
								</button>
							</form>
						</section>

						{#if task.worktree_path && ['completed', 'failed', 'ended', 'paused', 'blocked'].includes(task.status)}
							<section class="border-t border-gray-100 pt-3 dark:border-white/6">
								<button
									class="link text-xs text-red-600 dark:text-red-400"
									onclick={() => removeWorktree(task)}
								>
									{$t('coordination.removeWorktree')}
								</button>
								<span class="ml-1 text-[0.6875rem] text-gray-400"
									>{$t('coordination.removeWorktreeHint')}</span
								>
							</section>
						{/if}
						{#if ['completed', 'failed', 'ended'].includes(task.status) && !task.chat_missing}
							<section class="border-t border-gray-100 pt-3 dark:border-white/6">
								<button
									class="link text-xs text-red-600 dark:text-red-400"
									disabled={busy === `delete:${task.id}`}
									onclick={() => deleteTask(task)}
								>
									{$t('coordination.deleteTask')}
								</button>
								<span class="ml-1 text-[0.6875rem] text-gray-400"
									>{$t('coordination.deleteHint')}</span
								>
							</section>
						{/if}
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

{#if showNewTask}
	<Modal onclose={() => (showNewTask = false)} class="w-[min(32rem,calc(100vw-2rem))]">
		<form
			class="space-y-3 p-4 text-sm"
			onsubmit={(e) => {
				e.preventDefault();
				createTask();
			}}
		>
			<h2 class="text-base font-semibold text-gray-900 dark:text-gray-50">
				{$t('coordination.newTask')}
			</h2>
			<p class="text-xs text-gray-500">{$t('coordination.newTaskHint')}</p>
			{#if repoOptions.length > 1}
				<label class="block">
					<span class="label">{$t('coordination.repository')}</span>
					<select
						class="field"
						value={taskRoot}
						onchange={(e) => chooseTaskRepo((e.currentTarget as HTMLSelectElement).value)}
					>
						{#each repoOptions as repo (repo.path)}
							<option value={repo.path}>{repo.name} — {repo.path}</option>
						{/each}
					</select>
					<span class="mt-1 block text-[0.6875rem] text-gray-400">
						{$t('coordination.repositoryHint')}
					</span>
				</label>
			{/if}
			<label class="block">
				<span class="label">{$t('coordination.taskTitle')}</span>
				<input class="field" bind:value={form.title} required maxlength="200" />
			</label>
			<label class="block">
				<span class="label">{$t('coordination.description')}</span>
				<textarea class="field" rows="2" bind:value={form.description}></textarea>
			</label>
			<label class="block">
				<span class="label">{$t('coordination.startPrompt')}</span>
				<textarea
					class="field"
					rows="3"
					bind:value={form.startPrompt}
					placeholder={$t('coordination.startPromptHint')}
				></textarea>
			</label>
			{#if form.startPrompt.trim()}
				<label class="flex items-start gap-2 text-xs text-gray-600 dark:text-gray-300">
					<input type="checkbox" class="mt-0.5" bind:checked={form.startNow} />
					<span>
						{$t('coordination.startNow')}
						<span class="block text-[0.6875rem] text-gray-400"
							>{$t('coordination.startNowHint')}</span
						>
					</span>
				</label>
			{/if}
			<div class="flex items-center gap-2">
				<span class="label mb-0">{$t('coordination.model')}</span>
				<ModelSelector bind:selectedModel={form.model} preferAbove={false} align="start" />
			</div>
			<!-- The same per-chat options as the message box's + menu. -->
			<div class="flex flex-col gap-1.5">
				<label class="flex items-start gap-2 text-xs text-gray-600 dark:text-gray-300">
					<input type="checkbox" class="mt-0.5" bind:checked={form.planMode} />
					<span>
						{$t('coordination.planMode')}
						<span class="block text-[0.6875rem] text-gray-400"
							>{$t('coordination.planModeHint')}</span
						>
					</span>
				</label>
				<label class="flex items-start gap-2 text-xs text-gray-600 dark:text-gray-300">
					<input type="checkbox" class="mt-0.5" bind:checked={form.followEdits} />
					<span>
						{$t('coordination.followEdits')}
						<span class="block text-[0.6875rem] text-gray-400"
							>{$t('coordination.followEditsHint')}</span
						>
					</span>
				</label>
			</div>
			{#if baseOptions.length}
				<label class="block">
					<span class="label">{$t('coordination.startFrom')}</span>
					<select class="field font-mono" bind:value={form.base}>
						{#each baseOptions as name (name)}
							<option value={name}>{name}</option>
						{/each}
					</select>
					<span class="mt-1 block text-[0.6875rem] text-gray-400">
						{$t('coordination.startFromHint')}
					</span>
				</label>
			{/if}
			<label class="block">
				<span class="label">{$t('coordination.testCommand')}</span>
				<input class="field font-mono" bind:value={form.testCommand} placeholder="npm test" />
			</label>
			{#if !otherRepo && coord.tasks.some((task) => !['completed', 'failed', 'ended'].includes(task.status))}
				<fieldset>
					<legend class="label">{$t('coordination.dependsOnLabel')}</legend>
					<div class="max-h-32 space-y-1 overflow-y-auto">
						{#each coord.tasks.filter((task) => !['completed', 'failed', 'ended'].includes(task.status)) as other}
							<label class="flex items-center gap-2 text-xs">
								<input type="checkbox" value={other.id} bind:group={form.dependsOn} />
								<span class="truncate">{other.title}</span>
							</label>
						{/each}
					</div>
				</fieldset>
			{/if}
			<div class="flex justify-end gap-2 pt-1">
				<button type="button" class="btn-secondary" onclick={() => (showNewTask = false)}>
					{$t('coordination.cancel')}
				</button>
				<button
					type="submit"
					class="btn-primary"
					disabled={!form.title.trim() || busy === 'create'}
				>
					{#if busy === 'create'}<Spinner size={12} />{/if}
					{$t('coordination.create')}
				</button>
			</div>
		</form>
	</Modal>
{/if}

<style>
	.how-step {
		display: flex;
		gap: 0.5rem;
		align-items: flex-start;
		line-height: 1.35;
	}
	.how-num {
		display: inline-flex;
		flex-shrink: 0;
		align-items: center;
		justify-content: center;
		width: 1.125rem;
		height: 1.125rem;
		border-radius: 9999px;
		font-size: 0.625rem;
		font-weight: 600;
		background: rgb(229 231 235);
		color: rgb(55 65 81);
	}
	:global(.dark) .how-num {
		background: rgb(255 255 255 / 0.1);
		color: rgb(229 231 235);
	}
	.stat {
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		color: rgb(55 65 81);
	}
	:global(.dark) .stat {
		color: rgb(229 231 235);
	}
	.tile {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
		padding: 0.5rem 0.625rem;
		border-radius: 0.5rem;
		background: rgb(249 250 251);
	}
	:global(.dark) .tile {
		background: rgb(255 255 255 / 0.04);
	}
	.tile-value {
		font-size: 0.9375rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.tile-label {
		font-size: 0.6875rem;
		color: rgb(107 114 128);
	}
	.empty {
		display: flex;
		flex: 1;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 2rem 1rem;
		text-align: center;
		font-size: 0.8125rem;
		color: rgb(107 114 128);
	}
	.folder-scope {
		display: flex;
		gap: 0.25rem;
		padding: 0.5rem 0.75rem 0.125rem;
	}
	.folder-scope button {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		border-radius: 9999px;
		padding: 0.125rem 0.5rem;
		font-size: 0.6875rem;
		color: rgb(107 114 128);
		border: 1px solid rgb(229 231 235);
	}
	:global(.dark) .folder-scope button {
		border-color: rgb(255 255 255 / 0.08);
		color: rgb(156 163 175);
	}
	.folder-scope button.folder-scope-on {
		background: rgb(17 24 39);
		border-color: rgb(17 24 39);
		color: white;
	}
	:global(.dark) .folder-scope button.folder-scope-on {
		background: rgb(255 255 255 / 0.9);
		border-color: transparent;
		color: rgb(17 24 39);
	}
	.folder-chip {
		display: inline-flex;
		flex-shrink: 0;
		align-items: center;
		gap: 0.1875rem;
		max-width: 8rem;
		overflow: hidden;
		border-radius: 0.25rem;
		background: rgb(14 165 233 / 0.1);
		padding: 0 0.3125rem;
		font-size: 0.625rem;
		color: rgb(3 105 161);
		white-space: nowrap;
	}
	:global(.dark) .folder-chip {
		color: rgb(125 211 252);
	}
	.folder-chip.folder-chip-here {
		background: rgb(0 0 0 / 0.05);
		color: rgb(107 114 128);
	}
	.section-label {
		padding: 0.625rem 0.75rem 0.25rem;
		font-size: 0.625rem;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: rgb(156 163 175);
	}
	.section-title {
		margin-bottom: 0.375rem;
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: rgb(107 114 128);
	}
	.task-row {
		display: block;
		width: 100%;
		padding: 0.5rem 0.75rem;
		text-align: left;
		border-left: 2px solid transparent;
	}
	.task-row:hover {
		background: rgb(0 0 0 / 0.03);
	}
	.task-row-selected {
		background: rgb(0 0 0 / 0.045);
		border-left-color: rgb(59 130 246);
	}
	:global(.dark) .task-row:hover,
	:global(.dark) .task-row-selected {
		background: rgb(255 255 255 / 0.05);
	}
	.section-toggle {
		width: 100%;
		padding: 0.5rem 0.75rem;
		text-align: left;
		font-size: 0.6875rem;
		color: rgb(107 114 128);
	}
	.pill {
		flex-shrink: 0;
		border-radius: 9999px;
		padding: 0.0625rem 0.4375rem;
		font-size: 0.625rem;
		font-weight: 500;
		white-space: nowrap;
	}
	.facts {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 0.75rem;
		font-size: 0.75rem;
	}
	.facts dt {
		color: rgb(107 114 128);
	}
	.dep-line {
		display: flex;
		align-items: center;
		gap: 0.25rem;
	}
	.dep-remove {
		flex: none;
		border-radius: 0.25rem;
		padding: 0.25rem;
		color: rgb(107 114 128);
	}
	.dep-remove:hover:not(:disabled) {
		background: rgb(0 0 0 / 0.06);
		color: rgb(220 38 38);
	}
	.dep-remove:disabled {
		opacity: 0.4;
	}
	.dep-row {
		display: flex;
		min-width: 0;
		flex: 1;
		width: 100%;
		align-items: center;
		gap: 0.375rem;
		padding: 0.25rem 0;
		font-size: 0.75rem;
		text-align: left;
	}
	.output {
		margin-top: 0.375rem;
		max-height: 14rem;
		overflow: auto;
		border-radius: 0.25rem;
		background: rgb(0 0 0 / 0.04);
		padding: 0.5rem;
		font-size: 0.6875rem;
		white-space: pre-wrap;
	}
	:global(.dark) .output {
		background: rgb(255 255 255 / 0.05);
	}
	.btn-primary,
	.btn-secondary,
	.btn-danger,
	.icon-btn {
		display: inline-flex;
		align-items: center;
		gap: 0.3125rem;
		border-radius: 0.375rem;
		font-size: 0.75rem;
		line-height: 1rem;
		white-space: nowrap;
	}
	.btn-primary {
		padding: 0.3125rem 0.625rem;
		background: rgb(17 24 39);
		color: white;
	}
	:global(.dark) .btn-primary {
		background: rgb(243 244 246);
		color: rgb(17 24 39);
	}
	.btn-secondary {
		padding: 0.3125rem 0.625rem;
		border: 1px solid rgb(229 231 235);
		color: rgb(55 65 81);
	}
	:global(.dark) .btn-secondary {
		border-color: rgb(255 255 255 / 0.1);
		color: rgb(229 231 235);
	}
	.icon-btn {
		padding: 0.3125rem;
		color: rgb(107 114 128);
	}
	.next-step {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 0.75rem;
		padding: 0.625rem 0.75rem;
		border-radius: 0.5rem;
		font-size: 0.75rem;
		color: rgb(55 65 81);
		background: rgb(249 250 251);
		border: 1px solid rgb(229 231 235);
	}
	:global(.dark) .next-step {
		color: rgb(229 231 235);
		background: rgb(255 255 255 / 0.04);
		border-color: rgb(255 255 255 / 0.1);
	}
	.report {
		font-size: 0.75rem;
		color: rgb(75 85 99);
	}
	:global(.dark) .report {
		color: rgb(209 213 219);
	}
	.btn-danger {
		padding: 0.3125rem 0.625rem;
		background: rgb(220 38 38);
		color: white;
	}
	.chat-gone {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem 0.75rem;
		padding: 0.625rem 0.75rem;
		border-radius: 0.5rem;
		font-size: 0.75rem;
		color: rgb(75 85 99);
		background: rgb(249 250 251);
		border: 1px solid rgb(229 231 235);
	}
	:global(.dark) .chat-gone {
		color: rgb(209 213 219);
		background: rgb(255 255 255 / 0.04);
		border-color: rgb(255 255 255 / 0.1);
	}
	.chat-gone p {
		flex: 1 1 14rem;
		margin: 0;
	}
	.btn-primary:disabled,
	.btn-secondary:disabled,
	.btn-danger:disabled,
	.icon-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.link {
		color: rgb(37 99 235);
	}
	.link:hover {
		text-decoration: underline;
	}
	.label {
		display: block;
		margin-bottom: 0.25rem;
		font-size: 0.75rem;
		font-weight: 500;
		color: rgb(75 85 99);
	}
	:global(.dark) .label {
		color: rgb(209 213 219);
	}
	.field {
		width: 100%;
		border-radius: 0.375rem;
		border: 1px solid rgb(229 231 235);
		background: transparent;
		padding: 0.375rem 0.5rem;
		font-size: 0.8125rem;
	}
	:global(.dark) .field {
		border-color: rgb(255 255 255 / 0.1);
	}
</style>
