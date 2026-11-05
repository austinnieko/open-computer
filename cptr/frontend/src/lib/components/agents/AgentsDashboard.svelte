<script lang="ts">
	/**
	 * Agents dashboard: every task agent across repositories, grouped
	 * repository → folder (main checkout or worktree) → agent, with what needs
	 * attention first and usage totals. Rendered as the /agents route.
	 */
	import { onMount } from 'svelte';
	import { t } from '$lib/i18n';
	import { tooltip } from '$lib/tooltip';
	import { sidebarOpen } from '$lib/stores';
	import Icon from '../Icon.svelte';
	import Spinner from '../common/Spinner.svelte';
	import { compactNumber, compactDuration } from '$lib/stores/coordination.svelte';
	import {
		agentCounts,
		agentRows,
		agentsState,
		folderName,
		openAgentChat,
		openAgentsPanel,
		refreshAgents,
		trackAgents,
		type AgentRow,
		type AgentState
	} from '$lib/stores/agents.svelte';

	onMount(() => trackAgents({ usage: true }));

	const agents = agentsState();
	const rows = $derived(agentRows());
	const counts = $derived(agentCounts(rows));

	// ── Filters ────────────────────────────────────────────────
	type Filter = 'all' | 'attention' | 'running' | 'in_line' | 'review' | 'done';
	const FILTER_STATES: Record<Exclude<Filter, 'all'>, AgentState[]> = {
		attention: ['needs_you', 'blocked'],
		running: ['working', 'background'],
		in_line: ['in_line', 'stopped', 'draft'],
		review: ['review'],
		done: ['done', 'ended']
	};
	const FILTER_KEY = 'cptr:agents:filter';
	function readFilter(): Filter {
		try {
			const v = localStorage.getItem(FILTER_KEY);
			if (v && (v === 'all' || v in FILTER_STATES)) return v as Filter;
		} catch {
			// storage unavailable
		}
		return 'all';
	}
	let filter = $state<Filter>(readFilter());
	let query = $state('');
	function setFilter(next: Filter) {
		filter = filter === next ? 'all' : next;
		try {
			localStorage.setItem(FILTER_KEY, filter);
		} catch {
			// storage unavailable
		}
	}
	const countOf = (states: AgentState[]) => states.reduce((n, s) => n + (counts[s] ?? 0), 0);

	const visible = $derived.by(() => {
		const q = query.trim().toLowerCase();
		return rows.filter((r) => {
			if (filter !== 'all' && !FILTER_STATES[filter].includes(r.state)) return false;
			if (!q) return true;
			return [r.task.title, r.task.branch ?? '', r.folder, r.task.description ?? '']
				.join(' ')
				.toLowerCase()
				.includes(q);
		});
	});

	// ── Tree: repository → folder → agents ─────────────────────
	interface FolderGroup {
		path: string;
		main: boolean;
		rows: AgentRow[];
	}
	interface RepoGroup {
		path: string;
		key: string;
		folders: FolderGroup[];
		total: number;
		attention: number;
		running: number;
	}
	const tree = $derived.by((): RepoGroup[] => {
		const repos = new Map<string, RepoGroup>();
		for (const r of visible) {
			let repo = repos.get(r.task.repo_key);
			if (!repo) {
				repo = {
					path: r.repo,
					key: r.task.repo_key,
					folders: [],
					total: 0,
					attention: 0,
					running: 0
				};
				repos.set(r.task.repo_key, repo);
			}
			let folder = repo.folders.find((f) => f.path === r.folder);
			if (!folder) {
				folder = { path: r.folder, main: r.folder === r.repo, rows: [] };
				repo.folders.push(folder);
			}
			folder.rows.push(r);
			repo.total += 1;
			if (FILTER_STATES.attention.includes(r.state)) repo.attention += 1;
			if (FILTER_STATES.running.includes(r.state)) repo.running += 1;
		}
		for (const repo of repos.values()) {
			repo.folders.sort(
				(a, b) =>
					Number(b.main) - Number(a.main) || folderName(a.path).localeCompare(folderName(b.path))
			);
		}
		// Repositories needing you first, then busiest.
		return [...repos.values()].sort(
			(a, b) =>
				b.attention - a.attention ||
				b.running - a.running ||
				folderName(a.path).localeCompare(folderName(b.path))
		);
	});

	// Collapsed repositories and folders (remembered per browser).
	const COLLAPSE_KEY = 'cptr:agents:collapsed';
	function readCollapsed(): Set<string> {
		try {
			return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? '[]'));
		} catch {
			return new Set();
		}
	}
	let collapsed = $state<Set<string>>(readCollapsed());
	function toggle(key: string) {
		const next = new Set(collapsed);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		collapsed = next;
		try {
			localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...next]));
		} catch {
			// storage unavailable
		}
	}

	// ── Display helpers ────────────────────────────────────────
	const DOT: Record<AgentState, string> = {
		needs_you: 'bg-amber-500',
		blocked: 'bg-red-500',
		working: 'bg-blue-500 animate-pulse',
		background: 'bg-sky-400',
		in_line: 'bg-gray-400',
		stopped: 'bg-gray-300 dark:bg-gray-600',
		review: 'bg-violet-500',
		draft: 'border border-dashed border-gray-400',
		done: 'bg-emerald-500/70',
		ended: 'bg-gray-300 dark:bg-gray-600'
	};
	const PILL: Record<AgentState, string> = {
		needs_you: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
		blocked: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300',
		working: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300',
		background: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300',
		in_line: 'bg-gray-100 text-gray-600 dark:bg-white/8 dark:text-gray-300',
		stopped: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400',
		review: 'bg-violet-50 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300',
		draft:
			'border border-dashed border-gray-300 text-gray-500 dark:border-white/15 dark:text-gray-400',
		done: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
		ended: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400'
	};
	const labelOf = (s: AgentState) =>
		['review', 'draft', 'done', 'ended'].includes(s)
			? $t(`coordination.stage.${s}`)
			: $t(`coordination.activity.${s}`);
	function detailOf(r: AgentRow): string {
		const parts: string[] = [];
		if (r.life.detailKey && !['working'].includes(r.life.detailKey)) {
			parts.push($t(`coordination.detail.${r.life.detailKey}`, r.life.detailArgs ?? {}));
		}
		if (r.life.detailText) parts.push(r.life.detailText);
		return parts.join(' ');
	}
	function ago(ms: number): string {
		const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
		if (s < 60) return $t('home.agents.now');
		const m = Math.round(s / 60);
		if (m < 60) return `${m}m`;
		const h = Math.round(m / 60);
		return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`;
	}
	const money = (usd: number) => (usd >= 100 ? `$${Math.round(usd)}` : `$${usd.toFixed(2)}`);

	const METRICS: { filter: Exclude<Filter, 'all'>; states: AgentState[]; tone: string }[] = [
		{
			filter: 'attention',
			states: ['needs_you', 'blocked'],
			tone: 'text-amber-600 dark:text-amber-400'
		},
		{
			filter: 'running',
			states: ['working', 'background'],
			tone: 'text-blue-600 dark:text-blue-400'
		},
		{
			filter: 'in_line',
			states: ['in_line', 'stopped', 'draft'],
			tone: 'text-gray-700 dark:text-gray-200'
		},
		{ filter: 'review', states: ['review'], tone: 'text-violet-600 dark:text-violet-400' },
		{ filter: 'done', states: ['done', 'ended'], tone: 'text-emerald-600 dark:text-emerald-400' }
	];
	const SUB: Record<Exclude<Filter, 'all'>, AgentState[]> = {
		attention: ['needs_you', 'blocked'],
		running: ['working', 'background'],
		in_line: ['in_line', 'stopped', 'draft'],
		review: [],
		done: []
	};
	const openConflicts = $derived(Object.values(agents.conflicts).reduce((n, c) => n + c, 0));
	const active = $derived(rows.filter((r) => !['done', 'ended'].includes(r.state)).length);
</script>

<svelte:head>
	<title>{$t('agents.title')} / Computer</title>
</svelte:head>

<div class="flex h-full flex-col overflow-hidden">
	<!-- Header -->
	<div
		class="flex h-10 shrink-0 items-center gap-2 border-b border-gray-100 px-3 dark:border-white/6"
	>
		{#if !$sidebarOpen}
			<button
				class="flex size-6 items-center justify-center rounded-md text-gray-500 hover:bg-black/5 dark:hover:bg-white/6"
				onclick={() => sidebarOpen.set(true)}
				aria-label={$t('sidebar.open')}
			>
				<Icon name="sidebar-expand" size={14} />
			</button>
		{/if}
		<Icon name="cube" size={14} class="text-gray-500" />
		<h1 class="text-sm font-medium text-gray-800 dark:text-gray-100">{$t('agents.title')}</h1>
		<span class="whitespace-nowrap text-xs tabular-nums text-gray-400"
			>{$t('agents.activeCount', { count: active })}</span
		>
		<div class="ml-auto flex items-center gap-1.5">
			<div class="relative">
				<Icon
					name="search"
					size={12}
					class="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-gray-400"
				/>
				<input
					class="h-7 w-28 rounded-lg sm:w-44 border border-gray-200 bg-transparent pl-6 pr-2 text-xs outline-none placeholder:text-gray-400 focus:border-gray-300 dark:border-white/10 dark:focus:border-white/20"
					placeholder={$t('agents.search')}
					bind:value={query}
				/>
			</div>
			<button
				class="flex size-7 items-center justify-center rounded-lg text-gray-500 hover:bg-black/5 dark:hover:bg-white/6"
				onclick={() => refreshAgents()}
				aria-label={$t('agents.refresh')}
				use:tooltip={$t('agents.refresh')}
			>
				{#if agents.loading}<Spinner size={12} />{:else}<Icon name="refresh" size={13} />{/if}
			</button>
		</div>
	</div>

	<div class="flex-1 overflow-y-auto">
		<div class="mx-auto w-full max-w-5xl px-4 py-5 sm:px-6">
			{#if !agents.loaded}
				<div class="flex justify-center py-16"><Spinner size={20} /></div>
			{:else if !rows.length}
				<div class="py-16 text-center">
					<p class="text-sm text-gray-600 dark:text-gray-300">{$t('agents.emptyTitle')}</p>
					<p class="mt-1 text-xs text-gray-400">{$t('agents.emptyHint')}</p>
				</div>
			{:else}
				<!-- Metrics: each one filters the list -->
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-5">
					{#each METRICS as m (m.filter)}
						{@const n = countOf(m.states)}
						<button
							type="button"
							class="rounded-xl border px-3 py-2.5 text-left transition-colors duration-75 {filter ===
							m.filter
								? 'border-gray-300 bg-gray-50 dark:border-white/20 dark:bg-white/6'
								: 'border-gray-100 hover:bg-gray-50 dark:border-white/6 dark:hover:bg-white/4'}"
							aria-pressed={filter === m.filter}
							onclick={() => setFilter(m.filter)}
						>
							<div class="text-[0.6875rem] text-gray-500 dark:text-gray-400">
								{$t(`agents.metric.${m.filter}`)}
							</div>
							<div
								class="mt-0.5 text-xl font-medium tabular-nums {n
									? m.tone
									: 'text-gray-300 dark:text-gray-600'}"
							>
								{n}
							</div>
							{#if SUB[m.filter].length}
								<div
									class="mt-0.5 flex flex-wrap gap-x-2 text-[0.625rem] text-gray-400 dark:text-gray-500"
								>
									{#each SUB[m.filter].filter((s) => counts[s]) as s (s)}
										<span>{counts[s]} {labelOf(s).toLowerCase()}</span>
									{/each}
								</div>
							{/if}
						</button>
					{/each}
				</div>

				<!-- Usage -->
				<div
					class="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 px-1 text-xs text-gray-500 dark:text-gray-400"
				>
					{#if agents.usageTotals}
						<span use:tooltip={$t('agents.usageHint')}>
							<span class="tabular-nums text-gray-700 dark:text-gray-200"
								>{compactNumber(agents.usageTotals.tokens)}</span
							>
							{$t('agents.tokens')}
						</span>
						{#if agents.usageTotals.cost_usd > 0}
							<span>
								<span class="tabular-nums text-gray-700 dark:text-gray-200"
									>{money(agents.usageTotals.cost_usd)}</span
								>
								{$t('agents.cost')}
							</span>
						{/if}
						<span>
							<span class="tabular-nums text-gray-700 dark:text-gray-200"
								>{compactDuration(agents.usageTotals.duration_ms)}</span
							>
							{$t('agents.agentTime')}
						</span>
						<span>
							<span class="tabular-nums text-gray-700 dark:text-gray-200"
								>{compactNumber(agents.usageTotals.tool_calls)}</span
							>
							{$t('agents.toolCalls')}
						</span>
					{:else}
						<span class="inline-flex items-center gap-1.5"
							><Spinner size={10} />{$t('agents.loadingUsage')}</span
						>
					{/if}
					<span class:text-red-600={openConflicts > 0} class:dark:text-red-400={openConflicts > 0}>
						<span class="tabular-nums">{openConflicts}</span>
						{$t('agents.conflicts', { count: openConflicts })}
					</span>
					<span class="ml-auto text-gray-400">
						{$t('agents.slots', { max: agents.maxWriters })}
					</span>
				</div>

				{#if filter !== 'all' || query}
					<div class="mt-4 flex items-center gap-2 px-1 text-xs text-gray-500">
						<span>{$t('agents.showing', { count: visible.length, total: rows.length })}</span>
						<button
							class="text-gray-600 underline-offset-2 hover:underline dark:text-gray-300"
							onclick={() => {
								filter = 'all';
								query = '';
								try {
									localStorage.setItem(FILTER_KEY, 'all');
								} catch {
									// storage unavailable
								}
							}}>{$t('agents.clearFilter')}</button
						>
					</div>
				{/if}

				<!-- Repository → folder → agents -->
				<div class="mt-4 flex flex-col gap-4">
					{#each tree as repo (repo.key)}
						{@const repoOpen = !collapsed.has(repo.key)}
						<section class="overflow-hidden rounded-xl border border-gray-100 dark:border-white/6">
							<div class="flex items-center gap-2 bg-gray-50/70 px-3 py-2 dark:bg-white/3">
								<button
									class="flex min-w-0 flex-1 items-center gap-2 text-left"
									onclick={() => toggle(repo.key)}
									aria-expanded={repoOpen}
								>
									<Icon
										name="chevron-right"
										size={12}
										class="shrink-0 text-gray-400 transition-transform {repoOpen
											? 'rotate-90'
											: ''}"
									/>
									<Icon name="folder" size={14} class="shrink-0 text-gray-500" />
									<span class="max-w-[50%] shrink-0 truncate text-sm font-medium text-gray-800 dark:text-gray-100"
										>{folderName(repo.path)}</span
									>
									<span class="hidden min-w-0 truncate text-[0.6875rem] text-gray-400 sm:inline"
										>{repo.path}</span
									>
								</button>
								<span
									class="flex shrink-0 items-center gap-2 text-[0.6875rem] tabular-nums text-gray-500"
								>
									{#if repo.attention}
										<span class="text-amber-600 dark:text-amber-400"
											>{$t('agents.needAttention', { count: repo.attention })}</span
										>
									{/if}
									{#if repo.running}<span>{$t('agents.runningCount', { count: repo.running })}</span
										>{/if}
									<span class="text-gray-400">{$t('agents.taskCount', { count: repo.total })}</span>
								</span>
								<button
									class="shrink-0 rounded-md px-1.5 py-0.5 text-[0.6875rem] text-gray-500 hover:bg-black/5 hover:text-gray-800 dark:hover:bg-white/8 dark:hover:text-gray-200"
									onclick={() => openAgentsPanel(repo.path)}
									use:tooltip={$t('agents.openPanelHint')}
								>
									{$t('agents.openPanel')}
								</button>
							</div>

							{#if repoOpen}
								{#each repo.folders as folder (folder.path)}
									{@const fKey = `${repo.key}:${folder.path}`}
									{@const folderOpen = !collapsed.has(fKey)}
									<div class="border-t border-gray-100 dark:border-white/6">
										<div class="flex items-center gap-2 py-1.5 pl-3 pr-3 sm:pl-7">
											<button
												class="flex min-w-0 flex-1 items-center gap-1.5 text-left"
												onclick={() => toggle(fKey)}
												aria-expanded={folderOpen}
											>
												<Icon
													name="chevron-right"
													size={10}
													class="shrink-0 text-gray-400 transition-transform {folderOpen
														? 'rotate-90'
														: ''}"
												/>
												<Icon
													name={folder.main ? 'folder' : 'git-branch'}
													size={12}
													class="shrink-0 text-gray-400"
												/>
												<span class="truncate text-xs font-medium text-gray-700 dark:text-gray-200"
													>{folderName(folder.path)}</span
												>
												<span class="shrink-0 text-[0.625rem] text-gray-400"
													>{folder.main ? $t('agents.mainCheckout') : $t('agents.worktree')}</span
												>
												<span class="shrink-0 text-[0.625rem] tabular-nums text-gray-400"
													>· {folder.rows.length}</span
												>
											</button>
											{#if !folder.main}
												<button
													class="shrink-0 rounded-md px-1.5 py-0.5 text-[0.6875rem] text-gray-500 hover:bg-black/5 hover:text-gray-800 dark:hover:bg-white/8 dark:hover:text-gray-200"
													onclick={() => openAgentsPanel(folder.path)}
												>
													{$t('agents.openPanel')}
												</button>
											{/if}
										</div>
										{#if folderOpen}
											<ul>
												{#each folder.rows as r (r.task.id)}
													{@const detail = detailOf(r)}
													<li
														class="group flex items-center gap-2.5 py-2 pl-5 pr-3 sm:pl-12 hover:bg-gray-50 dark:hover:bg-white/4"
													>
														<span class="size-2 shrink-0 rounded-full {DOT[r.state]}"></span>
														<button
															class="min-w-0 flex-1 text-left"
															onclick={() => openAgentChat(r.task, r.folder)}
															use:tooltip={$t('agents.openChatHint')}
														>
															<span class="flex min-w-0 items-baseline gap-2">
																<span class="truncate text-sm text-gray-800 dark:text-gray-100"
																	>{r.task.title}</span
																>
																{#if r.task.branch}
																	<span
																		class="hidden shrink truncate font-mono text-[0.625rem] text-gray-400 md:inline"
																		>{r.task.branch}</span
																	>
																{/if}
															</span>
															{#if detail}
																<span
																	class="block truncate text-[0.6875rem] text-gray-500 dark:text-gray-400"
																	>{detail}</span
																>
															{/if}
														</button>
														{#if r.usage}
															<span
																class="hidden w-24 shrink-0 text-right text-[0.6875rem] tabular-nums text-gray-400 sm:inline"
																use:tooltip={$t('agents.rowUsage', {
																	turns: r.usage.turns,
																	tools: r.usage.tool_calls,
																	files: r.usage.files
																})}
															>
																{compactNumber(r.usage.tokens)} · {compactDuration(
																	r.usage.duration_ms
																)}
															</span>
														{/if}
														<span
															class="shrink-0 rounded-full px-2 py-0.5 text-[0.6875rem] font-medium {PILL[
																r.state
															]}">{labelOf(r.state)}</span
														>
														<span
															class="w-8 shrink-0 text-right text-[0.6875rem] tabular-nums text-gray-400"
															>{ago(r.task.updated_at)}</span
														>
														<button
															class="flex size-6 shrink-0 items-center justify-center rounded-md text-gray-400 opacity-0 hover:bg-black/5 hover:text-gray-700 group-hover:opacity-100 focus:opacity-100 dark:hover:bg-white/8 dark:hover:text-gray-200"
															onclick={() => openAgentsPanel(r.folder, r.task.id)}
															aria-label={$t('agents.details')}
															use:tooltip={$t('agents.details')}
														>
															<Icon name="info" size={13} />
														</button>
													</li>
												{/each}
											</ul>
										{/if}
									</div>
								{/each}
							{/if}
						</section>
					{/each}
					{#if !tree.length}
						<p class="py-8 text-center text-xs text-gray-400">{$t('agents.noMatch')}</p>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</div>
