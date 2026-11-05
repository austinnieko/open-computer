<script lang="ts">
	/**
	 * Home: a summary of task agents across every folder. Refetched on `coord:*`
	 * and `chat:active` socket events (debounced), never polled.
	 */
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import { workspaceList, addWorkspace } from '$lib/stores';
	import { socketStore } from '$lib/stores/socket.svelte';
	import { getCoordSummary, type CoordTask } from '$lib/apis/coordination';
	import { lifecycleOf, type TaskLifecycle } from '$lib/stores/coordination.svelte';

	/** compact: the welcome page's small list style instead of the chat landing's. */
	let { compact = false }: { compact?: boolean } = $props();

	let tasks = $state<CoordTask[]>([]);
	let maxWriters = $state(2);
	let loaded = $state(false);
	let timer: ReturnType<typeof setTimeout> | null = null;

	async function load() {
		try {
			const data = await getCoordSummary();
			tasks = data.tasks;
			maxWriters = data.limits?.max_writers ?? maxWriters;
		} catch {
			// Home still works without the summary.
		} finally {
			loaded = true;
		}
	}
	function soon(delay = 400) {
		if (timer) clearTimeout(timer);
		timer = setTimeout(() => void load(), delay);
	}

	onMount(() => {
		void load();
		const offEvents = socketStore.on('events:chat', (data: any) => {
			const type = data?.type;
			if (typeof type === 'string' && type.startsWith('coord:')) soon();
			else if (type === 'chat:active' && tasks.some((x) => x.chat_id === data.chat_id)) soon(1500);
		});
		const offConnect = socketStore.on('connect', () => soon(100));
		return () => {
			offEvents();
			offConnect();
			if (timer) clearTimeout(timer);
		};
	});

	// Most urgent first: what needs you, then what is moving, then the rest.
	const ORDER: Record<string, number> = {
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
	const SUMMARY_KEYS = ['needs_you', 'working', 'background', 'in_line', 'blocked', 'review'];
	const DOT: Record<string, string> = {
		needs_you: 'bg-amber-500',
		blocked: 'bg-red-500',
		working: 'bg-blue-500 animate-pulse',
		background: 'bg-sky-400',
		in_line: 'bg-gray-400',
		stopped: 'bg-gray-300 dark:bg-gray-600',
		review: 'bg-violet-500',
		draft: 'border border-dashed border-gray-400',
		done: 'bg-gray-300 dark:bg-gray-600',
		ended: 'bg-gray-300 dark:bg-gray-600'
	};

	const keyOf = (l: TaskLifecycle) => (l.stage === 'active' && l.activity ? l.activity : l.stage);
	const labelOf = (key: string) =>
		['review', 'draft', 'done', 'ended'].includes(key)
			? $t(`coordination.stage.${key}`)
			: $t(`coordination.activity.${key}`);
	const norm = (p: string) => p.replace(/\/+$/, '');
	const originOf = (task: CoordTask) =>
		norm(String(task.meta?.origin_root ?? task.repo_root ?? ''));
	const folderName = (p: string) => p.split('/').pop() || p;

	const rows = $derived.by(() => {
		const titles = new Map(tasks.map((x) => [x.id, x.title]));
		const runningWriters = tasks.filter((x) => x.status === 'running' && x.worktree_path).length;
		return tasks
			.map((task) => {
				const life = lifecycleOf(task, { titles, runningWriters, maxWriters });
				return { task, key: keyOf(life) };
			})
			.sort(
				(a, b) =>
					(ORDER[a.key] ?? 10) - (ORDER[b.key] ?? 10) || b.task.updated_at - a.task.updated_at
			);
	});
	const counts = $derived.by(() => {
		const c: Record<string, number> = {};
		for (const r of rows) c[r.key] = (c[r.key] ?? 0) + 1;
		return c;
	});
	const live = $derived(rows.filter((r) => !['done', 'ended'].includes(r.key)).length);

	let expanded = $state(false);
	const LIMIT = 6;
	const shown = $derived(expanded ? rows : rows.slice(0, LIMIT));

	function ago(ms: number): string {
		const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
		if (s < 60) return $t('home.agents.now');
		const m = Math.round(s / 60);
		if (m < 60) return `${m}m`;
		const h = Math.round(m / 60);
		return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`;
	}

	/** Open the task's chat in the folder it started from (where the sidebar lists it). */
	function open(task: CoordTask) {
		const origin = originOf(task);
		if (!origin) return;
		const ws = $workspaceList.find((w) => norm(w.path) === origin);
		if (!ws) addWorkspace(origin);
		const path = encodeURIComponent(ws?.path ?? origin);
		goto(
			task.chat_id && !task.chat_missing
				? `/?workspace=${path}&chatId=${encodeURIComponent(task.chat_id)}`
				: `/?workspace=${path}`
		);
	}
</script>

{#if loaded && rows.length}
	<section class={compact ? 'mb-4' : 'mt-6'} aria-label={$t('home.agents.title')}>
		<div class="flex items-baseline justify-between gap-2 px-1 mb-2">
			<h2
				class={compact
					? 'text-[0.6875rem] text-gray-400 dark:text-gray-600'
					: 'text-xs font-medium text-gray-500 dark:text-gray-400'}
			>
				{$t('home.agents.title')}
				<span class="ml-1 tabular-nums text-gray-400 dark:text-gray-500">{live}</span>
			</h2>
			<div
				class="flex flex-wrap justify-end gap-x-3 gap-y-1 text-[0.6875rem] text-gray-500 dark:text-gray-400"
			>
				{#each SUMMARY_KEYS.filter((k) => counts[k]) as key (key)}
					<span class="inline-flex items-center gap-1">
						<span class="size-1.5 rounded-full {DOT[key]}"></span>
						<span class="tabular-nums">{counts[key]}</span>
						{labelOf(key)}
					</span>
				{/each}
			</div>
		</div>
		<ul
			class="rounded-xl border border-gray-100 dark:border-white/6 divide-y divide-gray-100 dark:divide-white/6 overflow-hidden"
		>
			{#each shown as { task, key } (task.id)}
				<li>
					<button
						type="button"
						class="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-white/4 transition-colors duration-75"
						onclick={() => open(task)}
					>
						<span class="size-2 shrink-0 rounded-full {DOT[key] ?? 'bg-gray-300'}"></span>
						<span class="min-w-0 flex-1">
							<span class="block truncate text-sm text-gray-800 dark:text-gray-200"
								>{task.title}</span
							>
							<span class="block truncate text-[0.6875rem] text-gray-400 dark:text-gray-500">
								{folderName(originOf(task))}{task.branch ? ` · ${task.branch}` : ''}
							</span>
						</span>
						<span class="shrink-0 text-[0.6875rem] text-gray-500 dark:text-gray-400"
							>{labelOf(key)}</span
						>
						<span
							class="w-8 shrink-0 text-right text-[0.6875rem] tabular-nums text-gray-400 dark:text-gray-500"
						>
							{ago(task.updated_at)}
						</span>
					</button>
				</li>
			{/each}
		</ul>
		{#if rows.length > LIMIT}
			<button
				type="button"
				class="mt-1.5 px-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
				onclick={() => (expanded = !expanded)}
			>
				{expanded ? $t('home.agents.showLess') : $t('home.agents.showAll', { count: rows.length })}
			</button>
		{/if}
	</section>
{/if}
