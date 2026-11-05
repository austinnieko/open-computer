<script lang="ts">
	/**
	 * Agents dashboard: every task agent across repositories.
	 *
	 * Top: what needs you, the fleet at a glance (a bar of every agent by state
	 * and one card per group, each a filter), and usage. Below: the agents as a
	 * list (repository → folder → agent) or a board (one column per group).
	 * Agents expand in place for details, run their main action, and take a
	 * quick reply. Keyboard: / search, j/k move, Enter expand, o open chat,
	 * i details, 1-5 filter, b board/list, Esc clear.
	 */
	import { onMount, tick } from 'svelte';
	import { t } from '$lib/i18n';
	import { tooltip } from '$lib/tooltip';
	import { sidebarOpen } from '$lib/stores';
	import { socketStore } from '$lib/stores/socket.svelte';
	import Icon from '../Icon.svelte';
	import Spinner from '../common/Spinner.svelte';
	import AgentItem from './AgentItem.svelte';
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
		type AgentRow
	} from '$lib/stores/agents.svelte';
	import { GROUPS, GROUP_ACCENT, SWATCH, groupOf, type AgentGroup } from './ui';

	onMount(() => trackAgents({ usage: true }));

	// Relative times tick without refetching.
	let now = $state(Date.now());
	onMount(() => {
		const id = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(id);
	});

	const agents = agentsState();
	const rows = $derived(agentRows());
	const counts = $derived(agentCounts(rows));
	const titles = $derived(new Map(agents.tasks.map((x) => [x.id, x.title])));
	const byId = $derived(new Map(agents.tasks.map((x) => [x.id, x])));
	const taskOf = (id: string) => byId.get(id);
	const groupCount = (g: AgentGroup) => rows.filter((r) => groupOf(r.state) === g).length;

	// ── Remembered view settings ───────────────────────────────
	function readPref<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
		try {
			const v = localStorage.getItem(key) as T | null;
			if (v && allowed.includes(v)) return v;
		} catch {
			// storage unavailable
		}
		return fallback;
	}
	function writePref(key: string, value: string) {
		try {
			localStorage.setItem(key, value);
		} catch {
			// storage unavailable
		}
	}
	const FILTERS = ['all', 'attention', 'running', 'waiting', 'review', 'done'] as const;
	type Filter = (typeof FILTERS)[number];
	let filter = $state<Filter>(readPref('cptr:agents:filter', FILTERS, 'all'));
	let view = $state<'list' | 'board'>(readPref('cptr:agents:view', ['list', 'board'], 'list'));
	let query = $state('');
	let searchEl = $state<HTMLInputElement | null>(null);

	function setFilter(next: Filter) {
		filter = filter === next ? 'all' : next;
		writePref('cptr:agents:filter', filter);
	}
	function setView(next: 'list' | 'board') {
		view = next;
		writePref('cptr:agents:view', next);
	}

	const matches = (r: AgentRow, q: string) =>
		!q ||
		[r.task.title, r.task.branch ?? '', r.folder, r.task.description ?? '']
			.join(' ')
			.toLowerCase()
			.includes(q);
	const visible = $derived.by(() => {
		const q = query.trim().toLowerCase();
		return rows.filter((r) => (filter === 'all' || groupOf(r.state) === filter) && matches(r, q));
	});

	// ── List: repository → folder → agents ─────────────────────
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
			const g = groupOf(r.state);
			if (g === 'attention') repo.attention += 1;
			if (g === 'running') repo.running += 1;
		}
		for (const repo of repos.values()) {
			repo.folders.sort(
				(a, b) =>
					Number(b.main) - Number(a.main) || folderName(a.path).localeCompare(folderName(b.path))
			);
		}
		return [...repos.values()].sort(
			(a, b) =>
				b.attention - a.attention ||
				b.running - a.running ||
				folderName(a.path).localeCompare(folderName(b.path))
		);
	});

	// ── Board: one column per group ────────────────────────────
	const columns = $derived(
		GROUPS.filter((g) => filter === 'all' || g.key === filter).map((g) => ({
			...g,
			rows: visible.filter((r) => groupOf(r.state) === g.key)
		}))
	);

	// Collapsed repositories and folders (list view).
	let collapsed = $state<Set<string>>(
		(() => {
			try {
				return new Set<string>(JSON.parse(localStorage.getItem('cptr:agents:collapsed') ?? '[]'));
			} catch {
				return new Set<string>();
			}
		})()
	);
	function toggleCollapsed(key: string) {
		const next = new Set(collapsed);
		if (next.has(key)) next.delete(key);
		else next.add(key);
		collapsed = next;
		writePref('cptr:agents:collapsed', JSON.stringify([...next]));
	}

	// ── Selection, expansion, keyboard ─────────────────────────
	let selectedId = $state<string | null>(null);
	let expanded = $state<Set<string>>(new Set());
	function toggleExpanded(id: string) {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		expanded = next;
	}
	/** Agents in the order they appear on screen, for j/k. */
	const ordered = $derived.by((): AgentRow[] =>
		view === 'board'
			? columns.flatMap((c) => c.rows)
			: tree.flatMap((repo) =>
					collapsed.has(repo.key)
						? []
						: repo.folders.flatMap((f) => (collapsed.has(`${repo.key}:${f.path}`) ? [] : f.rows))
				)
	);
	async function move(delta: number) {
		if (!ordered.length) return;
		const i = ordered.findIndex((r) => r.task.id === selectedId);
		const next = ordered[Math.min(ordered.length - 1, Math.max(0, i < 0 ? 0 : i + delta))];
		selectedId = next.task.id;
		await tick();
		document
			.querySelector(`[data-agent-id="${next.task.id}"]`)
			?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
	}
	function onKey(e: KeyboardEvent) {
		const el = e.target as HTMLElement | null;
		const typing =
			el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
		if (typing) {
			if (e.key === 'Escape' && el === searchEl) {
				query = '';
				searchEl?.blur();
			}
			return;
		}
		if (e.metaKey || e.ctrlKey || e.altKey) return;
		const selected = ordered.find((r) => r.task.id === selectedId);
		switch (e.key) {
			case '/':
				e.preventDefault();
				searchEl?.focus();
				return;
			case 'j':
			case 'ArrowDown':
				e.preventDefault();
				void move(1);
				return;
			case 'k':
			case 'ArrowUp':
				e.preventDefault();
				void move(-1);
				return;
			case 'Enter':
			case ' ':
				if (selected) {
					e.preventDefault();
					toggleExpanded(selected.task.id);
				}
				return;
			case 'o':
				if (selected) openAgentChat(selected.task, selected.folder);
				return;
			case 'i':
				if (selected) openAgentsPanel(selected.folder, selected.task.id);
				return;
			case 'b':
				setView(view === 'board' ? 'list' : 'board');
				return;
			case 'Escape':
				if (filter !== 'all' || query) {
					filter = 'all';
					query = '';
					writePref('cptr:agents:filter', 'all');
				} else selectedId = null;
				return;
		}
		const n = Number(e.key);
		if (n >= 1 && n <= GROUPS.length) setFilter(GROUPS[n - 1].key);
	}

	// ── What needs you ─────────────────────────────────────────
	const attention = $derived(rows.filter((r) => groupOf(r.state) === 'attention'));
	async function jumpTo(r: AgentRow) {
		if (filter !== 'all' && filter !== 'attention') {
			filter = 'all';
			writePref('cptr:agents:filter', 'all');
		}
		query = '';
		collapsed = new Set(
			[...collapsed].filter((k) => k !== r.task.repo_key && k !== `${r.task.repo_key}:${r.folder}`)
		);
		selectedId = r.task.id;
		if (!expanded.has(r.task.id)) toggleExpanded(r.task.id);
		await tick();
		const el = document.querySelector(`[data-agent-id="${r.task.id}"]`);
		el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
		(el?.querySelector('textarea') as HTMLTextAreaElement | null)?.focus({ preventScroll: true });
	}

	// ── Fleet numbers ──────────────────────────────────────────
	const total = $derived(rows.length);
	const STATE_SEQUENCE = GROUPS.flatMap((g) => g.states);
	const segments = $derived(
		STATE_SEQUENCE.filter((s) => counts[s]).map((s) => ({ state: s, n: counts[s]! }))
	);
	const writing = $derived(
		rows.filter((r) => r.state === 'working' && r.task.worktree_path).length
	);
	const openConflicts = $derived(Object.values(agents.conflicts).reduce((n, c) => n + c, 0));
	const money = (usd: number) => (usd >= 100 ? `$${Math.round(usd)}` : `$${usd.toFixed(2)}`);
	const labelOf = (s: string) =>
		['review', 'draft', 'done', 'ended'].includes(s)
			? $t(`coordination.stage.${s}`)
			: $t(`coordination.activity.${s}`);
	const attentionLabel = (r: AgentRow) =>
		r.life.detailKey === 'dependencyGone'
			? $t('agents.fix')
			: r.task.chat_id &&
				  !r.task.chat_missing &&
				  ['question', 'agentBlocked', 'noReport'].includes(r.life.detailKey ?? '')
				? $t('agents.answer')
				: $t('agents.look');
	const attentionText = (r: AgentRow) =>
		r.life.detailText ||
		(r.life.detailKey
			? $t(`coordination.detail.${r.life.detailKey}`, r.life.detailArgs ?? {})
			: '');
</script>

<svelte:head>
	<title>{$t('agents.title')} / Computer</title>
</svelte:head>
<svelte:window onkeydown={onKey} />

<div class="flex h-full flex-col overflow-hidden">
	<!-- Header -->
	<header
		class="flex h-11 shrink-0 items-center gap-2 border-b border-gray-100 px-3 dark:border-white/6"
	>
		{#if !$sidebarOpen}
			<button
				class="icon-btn"
				onclick={() => sidebarOpen.set(true)}
				aria-label={$t('automations.toggleSidebar')}
			>
				<Icon name="sidebar-expand" size={14} />
			</button>
		{/if}
		<h1 class="text-sm font-semibold tracking-tight text-gray-900 dark:text-gray-50">
			{$t('agents.title')}
		</h1>
		<span
			class="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[0.625rem] font-medium {socketStore.connected
				? 'text-emerald-600 dark:text-emerald-400'
				: 'text-gray-400'}"
			use:tooltip={socketStore.connected ? $t('agents.liveHint') : $t('agents.offlineHint')}
		>
			<span class="relative flex size-1.5">
				{#if socketStore.connected}
					<span class="absolute inline-flex size-full animate-ping rounded-full bg-emerald-500/60"
					></span>
				{/if}
				<span
					class="relative inline-flex size-1.5 rounded-full {socketStore.connected
						? 'bg-emerald-500'
						: 'bg-gray-400'}"
				></span>
			</span>
			{socketStore.connected ? $t('agents.live') : $t('agents.offline')}
		</span>

		<div class="ml-auto flex items-center gap-1.5">
			<div class="relative">
				<Icon
					name="search"
					size={12}
					class="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-gray-400"
				/>
				<input
					bind:this={searchEl}
					class="search h-7 w-28 rounded-lg pr-6 pl-6 text-xs outline-none placeholder:text-gray-400 sm:w-52"
					placeholder={$t('agents.search')}
					bind:value={query}
				/>
				<kbd class="kbd kbd-wide pointer-events-none absolute top-1/2 right-1.5 -translate-y-1/2"
					>/</kbd
				>
			</div>
			<div class="segmented" role="group" aria-label={$t('agents.view')}>
				<button
					class:on={view === 'list'}
					onclick={() => setView('list')}
					use:tooltip={$t('agents.viewList')}
					aria-label={$t('agents.viewList')}><Icon name="list" size={13} /></button
				>
				<button
					class:on={view === 'board'}
					onclick={() => setView('board')}
					use:tooltip={$t('agents.viewBoard')}
					aria-label={$t('agents.viewBoard')}><Icon name="split-view" size={13} /></button
				>
			</div>
			<button
				class="icon-btn"
				onclick={() => refreshAgents()}
				aria-label={$t('agents.refresh')}
				use:tooltip={$t('agents.refresh')}
			>
				{#if agents.loading}<Spinner size={12} />{:else}<Icon name="refresh" size={13} />{/if}
			</button>
		</div>
	</header>

	<div class="flex-1 overflow-y-auto">
		<div
			class="mx-auto w-full px-4 py-5 sm:px-6 {view === 'board' ? 'max-w-[90rem]' : 'max-w-5xl'}"
		>
			{#if !agents.loaded}
				<div class="flex justify-center py-20"><Spinner size={20} /></div>
			{:else if !rows.length}
				<div class="flex flex-col items-center py-20 text-center">
					<div
						class="mb-3 flex size-10 items-center justify-center rounded-2xl bg-gray-100 text-gray-500 dark:bg-white/6"
					>
						<Icon name="cube" size={18} />
					</div>
					<p class="text-sm font-medium text-gray-700 dark:text-gray-200">
						{$t('agents.emptyTitle')}
					</p>
					<p class="mt-1 max-w-sm text-xs text-gray-400">{$t('agents.emptyHint')}</p>
				</div>
			{:else}
				<!-- What needs you -->
				{#if attention.length}
					<section
						class="mb-4 overflow-hidden rounded-2xl border border-amber-500/25 bg-gradient-to-br from-amber-500/[0.09] via-amber-500/[0.03] to-transparent"
					>
						<div class="flex items-center gap-2 px-4 pt-3 pb-1.5">
							<span
								class="flex size-5 items-center justify-center rounded-full bg-amber-500/20 text-amber-700 dark:text-amber-300"
								><Icon name="info" size={12} /></span
							>
							<h2 class="text-xs font-semibold text-amber-800 dark:text-amber-200">
								{$t('agents.needsYouTitle', { count: attention.length })}
							</h2>
						</div>
						<ul class="divide-y divide-amber-500/10">
							{#each attention.slice(0, 4) as r (r.task.id)}
								<li>
									<button
										class="flex w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-amber-500/[0.06]"
										onclick={() => jumpTo(r)}
									>
										<span class="size-2 shrink-0 rounded-full {SWATCH[r.state]}"></span>
										<span class="min-w-0 flex-1">
											<span
												class="block truncate text-[0.8125rem] font-medium text-gray-900 dark:text-gray-50"
												>{r.task.title}
												<span class="ml-1 text-[0.6875rem] font-normal text-gray-500"
													>{folderName(r.folder)}</span
												></span
											>
											<span class="block truncate text-[0.6875rem] text-gray-600 dark:text-gray-300"
												>{attentionText(r)}</span
											>
										</span>
										<span
											class="shrink-0 rounded-lg bg-amber-500/15 px-2 py-1 text-[0.6875rem] font-medium text-amber-800 dark:text-amber-200"
											>{attentionLabel(r)}</span
										>
									</button>
								</li>
							{/each}
						</ul>
						{#if attention.length > 4}
							<button
								class="w-full px-4 py-1.5 text-left text-[0.6875rem] text-amber-700 hover:underline dark:text-amber-300"
								onclick={() => setFilter('attention')}
								>{$t('agents.moreAttention', { count: attention.length - 4 })}</button
							>
						{/if}
					</section>
				{/if}

				<!-- Fleet bar -->
				<div class="mb-3">
					<div class="mb-1.5 flex items-baseline justify-between gap-2">
						<span class="text-[0.6875rem] text-gray-500 dark:text-gray-400">
							<span class="text-base font-semibold tabular-nums text-gray-900 dark:text-gray-50"
								>{total}</span
							>
							{$t('agents.agentsTotal', { count: total })}
						</span>
						{#if !attention.length}
							<span
								class="inline-flex items-center gap-1 text-[0.6875rem] text-emerald-600 dark:text-emerald-400"
								><Icon name="check" size={12} />{$t('agents.allClear')}</span
							>
						{/if}
					</div>
					<div class="flex h-2.5 w-full gap-0.5 overflow-hidden rounded-full">
						{#each segments as seg (seg.state)}
							<button
								class="fleet-seg h-full first:rounded-l-full last:rounded-r-full {SWATCH[
									seg.state
								]} {filter !== 'all' && groupOf(seg.state) !== filter ? 'opacity-25' : ''}"
								style="flex-grow: {seg.n}; flex-basis: 0"
								onclick={() => setFilter(groupOf(seg.state))}
								use:tooltip={`${seg.n} ${labelOf(seg.state)}`}
								aria-label={`${seg.n} ${labelOf(seg.state)}`}
							></button>
						{/each}
					</div>
				</div>

				<!-- Group cards: each one filters -->
				<div class="grid grid-cols-2 gap-2 sm:grid-cols-5">
					{#each GROUPS as g, i (g.key)}
						{@const n = groupCount(g.key)}
						{@const accent = GROUP_ACCENT[g.key]}
						<button
							type="button"
							class="metric relative overflow-hidden rounded-xl border px-3 py-2.5 text-left {filter ===
							g.key
								? 'border-gray-400/70 ring-2 ring-gray-900/5 dark:border-white/25 dark:ring-white/10'
								: 'border-gray-200/80 hover:border-gray-300 dark:border-white/8 dark:hover:border-white/15'} {i ===
							4
								? 'col-span-2 sm:col-span-1'
								: ''}"
							aria-pressed={filter === g.key}
							onclick={() => setFilter(g.key)}
						>
							<span
								class="pointer-events-none absolute inset-0 bg-gradient-to-br {accent.glow} to-transparent transition-opacity duration-200 {n
									? 'opacity-100'
									: 'opacity-0'}"
							></span>
							<span class="relative flex items-center justify-between">
								<span class="text-[0.6875rem] font-medium text-gray-500 dark:text-gray-400"
									>{$t(`agents.group.${g.key}`)}</span
								>
								<kbd class="kbd kbd-wide">{i + 1}</kbd>
							</span>
							<span
								class="relative mt-1 block text-2xl font-semibold tracking-tight tabular-nums {n
									? accent.text
									: 'text-gray-300 dark:text-gray-600'}">{n}</span
							>
							<span
								class="relative mt-1 flex min-h-3.5 flex-wrap gap-x-2 text-[0.625rem] text-gray-400 dark:text-gray-500"
							>
								{#each g.states.filter((s) => counts[s]) as s (s)}
									<span class="inline-flex items-center gap-1"
										><span class="size-1.5 rounded-full {SWATCH[s]}"></span>{counts[s]}
										{labelOf(s).toLowerCase()}</span
									>
								{/each}
							</span>
						</button>
					{/each}
				</div>

				<!-- Usage: same five columns and gaps as the cards above, so edges line up -->
				<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
					<div class="stat">
						<span class="stat-label">{$t('agents.tokens')}</span>
						<span class="stat-value"
							>{agents.usageTotals ? compactNumber(agents.usageTotals.tokens) : '–'}</span
						>
					</div>
					<div class="stat">
						<span class="stat-label">{$t('agents.cost')}</span>
						<span class="stat-value"
							>{agents.usageTotals ? money(agents.usageTotals.cost_usd) : '–'}</span
						>
					</div>
					<div class="stat">
						<span class="stat-label">{$t('agents.agentTime')}</span>
						<span class="stat-value"
							>{agents.usageTotals ? compactDuration(agents.usageTotals.duration_ms) : '–'}</span
						>
						{#if agents.usageTotals}
							<span class="stat-sub"
								>{compactNumber(agents.usageTotals.tool_calls)} {$t('agents.toolCalls')}</span
							>
						{/if}
					</div>
					<div class="stat">
						<span class="stat-label">{$t('agents.conflictsLabel')}</span>
						<span class="stat-value {openConflicts ? 'text-red-600 dark:text-red-400' : ''}"
							>{openConflicts}</span
						>
					</div>
					<div
						class="stat col-span-2 sm:col-span-1"
						use:tooltip={$t('agents.slots', { max: agents.maxWriters })}
					>
						<span class="stat-label">{$t('agents.writing')}</span>
						<span class="stat-value"
							>{writing}<span class="font-normal text-gray-400">/{agents.maxWriters}</span></span
						>
						<span class="mt-1.5 flex gap-0.5">
							{#each Array(agents.maxWriters) as _, i (i)}
								<span
									class="h-1 flex-1 rounded-full {i < writing
										? 'bg-blue-500'
										: 'bg-gray-200 dark:bg-white/10'}"
								></span>
							{/each}
						</span>
					</div>
				</div>

				{#if filter !== 'all' || query}
					<div class="mt-4 flex items-center gap-2 text-xs text-gray-500">
						{#if filter !== 'all'}
							<span
								class="inline-flex items-center gap-1 rounded-full bg-gray-100 py-0.5 pr-1 pl-2 text-gray-700 dark:bg-white/8 dark:text-gray-200"
								>{$t(`agents.group.${filter}`)}
								<button
									class="flex size-4 items-center justify-center rounded-full hover:bg-black/10 dark:hover:bg-white/10"
									onclick={() => setFilter(filter)}
									aria-label={$t('agents.clearFilter')}><Icon name="xmark" size={9} /></button
								></span
							>
						{/if}
						<span>{$t('agents.showing', { count: visible.length, total: rows.length })}</span>
					</div>
				{/if}

				<!-- Agents -->
				{#if view === 'list'}
					<div class="mt-4 flex flex-col gap-3">
						{#each tree as repo (repo.key)}
							{@const repoOpen = !collapsed.has(repo.key)}
							<section
								class="overflow-hidden rounded-2xl border border-gray-200/80 dark:border-white/8"
							>
								<div class="flex items-center gap-2 px-3 py-2.5">
									<button
										class="flex min-w-0 flex-1 items-center gap-2 text-left"
										onclick={() => toggleCollapsed(repo.key)}
										aria-expanded={repoOpen}
									>
										<Icon
											name="chevron-right"
											size={12}
											class="shrink-0 text-gray-400 transition-transform duration-150 {repoOpen
												? 'rotate-90'
												: ''}"
										/>
										<span
											class="flex size-6 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-gray-600 dark:bg-white/8 dark:text-gray-300"
											><Icon name="folder" size={13} /></span
										>
										<span
											class="max-w-[50%] shrink-0 truncate text-sm font-semibold text-gray-900 dark:text-gray-50"
											>{folderName(repo.path)}</span
										>
										<span class="hidden min-w-0 truncate text-[0.6875rem] text-gray-400 md:inline"
											>{repo.path}</span
										>
									</button>
									<span class="flex shrink-0 items-center gap-1.5 text-[0.6875rem] tabular-nums">
										{#if repo.attention}
											<span class="chip bg-amber-500/12 text-amber-700 dark:text-amber-300"
												>{repo.attention} {$t('agents.chipNeedYou')}</span
											>
										{/if}
										{#if repo.running}
											<span class="chip bg-blue-500/12 text-blue-700 dark:text-blue-300"
												>{repo.running} {$t('agents.chipRunning')}</span
											>
										{/if}
										<span class="hidden text-gray-400 sm:inline"
											>{$t('agents.taskCount', { count: repo.total })}</span
										>
									</span>
									<button
										class="link-btn"
										onclick={() => openAgentsPanel(repo.path)}
										use:tooltip={$t('agents.openPanelHint')}>{$t('agents.openPanel')}</button
									>
								</div>

								{#if repoOpen}
									{#each repo.folders as folder (folder.path)}
										{@const fKey = `${repo.key}:${folder.path}`}
										{@const folderOpen = !collapsed.has(fKey)}
										<div class="border-t border-gray-100 dark:border-white/6">
											<div class="flex items-center gap-2 py-1.5 pr-3 pl-3 sm:pl-7">
												<button
													class="flex min-w-0 flex-1 items-center gap-1.5 text-left"
													onclick={() => toggleCollapsed(fKey)}
													aria-expanded={folderOpen}
												>
													<Icon
														name="chevron-right"
														size={10}
														class="shrink-0 text-gray-400 transition-transform duration-150 {folderOpen
															? 'rotate-90'
															: ''}"
													/>
													<Icon
														name={folder.main ? 'folder' : 'git-branch'}
														size={12}
														class="shrink-0 {folder.main ? 'text-gray-400' : 'text-violet-500'}"
													/>
													<span
														class="truncate text-xs font-medium text-gray-700 dark:text-gray-200"
														>{folderName(folder.path)}</span
													>
													<span
														class="shrink-0 rounded-full px-1.5 text-[0.625rem] {folder.main
															? 'bg-gray-100 text-gray-500 dark:bg-white/6'
															: 'bg-violet-500/10 text-violet-600 dark:text-violet-300'}"
														>{folder.main ? $t('agents.mainCheckout') : $t('agents.worktree')}</span
													>
													<span class="shrink-0 text-[0.625rem] tabular-nums text-gray-400"
														>{folder.rows.length}</span
													>
													<span
														class="ml-1 hidden h-1.5 w-16 gap-px overflow-hidden rounded-full sm:flex"
													>
														{#each folder.rows as r (r.task.id)}
															<span class="h-full flex-1 {SWATCH[r.state]}"></span>
														{/each}
													</span>
												</button>
												{#if !folder.main}
													<button class="link-btn" onclick={() => openAgentsPanel(folder.path)}
														>{$t('agents.openPanel')}</button
													>
												{/if}
											</div>
											{#if folderOpen}
												<div class="pb-1">
													{#each folder.rows as r (r.task.id)}
														<AgentItem
															row={r}
															{titles}
															{now}
															dependsOn={agents.dependsOn[r.task.id] ?? []}
															{taskOf}
															selected={selectedId === r.task.id}
															expanded={expanded.has(r.task.id)}
															onselect={() => (selectedId = r.task.id)}
															ontoggle={() => toggleExpanded(r.task.id)}
														/>
													{/each}
												</div>
											{/if}
										</div>
									{/each}
								{/if}
							</section>
						{/each}
					</div>
				{:else}
					<div
						class="mt-4 grid items-start gap-3 {filter === 'all'
							? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-5'
							: 'grid-cols-1'}"
					>
						{#each columns as col (col.key)}
							{@const accent = GROUP_ACCENT[col.key]}
							<div class="flex min-w-0 flex-col rounded-2xl bg-gray-50/80 p-2 dark:bg-white/[0.02]">
								<div class="flex items-center gap-1.5 px-1.5 pt-0.5 pb-2">
									<span class="h-3 w-1 rounded-full {accent.bar}"></span>
									<span class="text-xs font-semibold text-gray-700 dark:text-gray-200"
										>{$t(`agents.group.${col.key}`)}</span
									>
									<span class="text-[0.6875rem] tabular-nums text-gray-400">{col.rows.length}</span>
								</div>
								<div
									class="flex flex-col gap-2 {filter !== 'all'
										? 'sm:grid sm:grid-cols-2 lg:grid-cols-3'
										: ''}"
								>
									{#each col.rows as r (r.task.id)}
										<AgentItem
											row={r}
											variant="card"
											{titles}
											{now}
											dependsOn={agents.dependsOn[r.task.id] ?? []}
											{taskOf}
											selected={selectedId === r.task.id}
											expanded={expanded.has(r.task.id)}
											onselect={() => (selectedId = r.task.id)}
											ontoggle={() => toggleExpanded(r.task.id)}
										/>
									{:else}
										<p class="px-2 py-4 text-center text-[0.6875rem] text-gray-400">
											{$t('agents.emptyColumn')}
										</p>
									{/each}
								</div>
							</div>
						{/each}
					</div>
				{/if}
				{#if !visible.length}
					<p class="py-10 text-center text-xs text-gray-400">{$t('agents.noMatch')}</p>
				{/if}

				<!-- Keyboard -->
				<p
					class="mt-6 hidden flex-wrap justify-center gap-x-3 gap-y-1 text-[0.625rem] text-gray-400 sm:flex"
				>
					<span><kbd class="kbd">/</kbd> {$t('agents.keySearch')}</span>
					<span><kbd class="kbd">j</kbd><kbd class="kbd">k</kbd> {$t('agents.keyMove')}</span>
					<span><kbd class="kbd">↵</kbd> {$t('agents.keyExpand')}</span>
					<span><kbd class="kbd">o</kbd> {$t('agents.keyChat')}</span>
					<span><kbd class="kbd">i</kbd> {$t('agents.details')}</span>
					<span><kbd class="kbd">1</kbd>–<kbd class="kbd">5</kbd> {$t('agents.keyFilter')}</span>
					<span><kbd class="kbd">b</kbd> {$t('agents.keyView')}</span>
				</p>
			{/if}
		</div>
	</div>
</div>

<style>
	.icon-btn {
		display: flex;
		width: 1.75rem;
		height: 1.75rem;
		align-items: center;
		justify-content: center;
		border-radius: 0.5rem;
		color: rgb(107 114 128);
		transition: background-color 0.1s;
	}
	.icon-btn:hover {
		background: color-mix(in oklab, var(--app-fg) 6%, transparent);
	}
	.search {
		background: color-mix(in oklab, var(--app-fg) 4%, transparent);
		border: 1px solid transparent;
		transition:
			border-color 0.1s,
			background-color 0.1s;
	}
	.search:focus {
		background: transparent;
		border-color: color-mix(in oklab, var(--app-fg) 18%, transparent);
	}
	.kbd {
		display: inline-flex;
		min-width: 1rem;
		height: 1rem;
		align-items: center;
		justify-content: center;
		padding: 0 0.2rem;
		margin: 0 0.05rem;
		border-radius: 0.25rem;
		font-family: ui-monospace, monospace;
		font-size: 0.5625rem;
		color: rgb(156 163 175);
		border: 1px solid color-mix(in oklab, var(--app-fg) 12%, transparent);
	}
	@media (max-width: 639px) {
		.kbd-wide {
			display: none;
		}
	}
	.segmented {
		display: flex;
		padding: 2px;
		border-radius: 0.5rem;
		background: color-mix(in oklab, var(--app-fg) 5%, transparent);
	}
	.segmented button {
		display: flex;
		width: 1.625rem;
		height: 1.375rem;
		align-items: center;
		justify-content: center;
		border-radius: 0.375rem;
		color: rgb(156 163 175);
		transition:
			background-color 0.1s,
			color 0.1s;
	}
	.segmented button.on {
		color: var(--app-fg);
		background: var(--app-bg);
		box-shadow: 0 1px 2px rgb(0 0 0 / 0.12);
	}
	.fleet-seg {
		min-width: 4px;
		transition:
			flex-grow 0.4s cubic-bezier(0.2, 0.8, 0.2, 1),
			opacity 0.15s,
			filter 0.1s;
	}
	.fleet-seg:hover {
		filter: brightness(1.15);
	}
	.metric {
		transition:
			border-color 0.1s,
			transform 0.12s;
	}
	.metric:active {
		transform: scale(0.985);
	}
	.stat {
		display: flex;
		flex-direction: column;
		padding: 0.625rem 0.75rem;
		border-radius: 0.75rem;
		border: 1px solid color-mix(in oklab, var(--app-fg) 9%, transparent);
	}
	.stat-sub {
		margin-top: 0.1rem;
		font-size: 0.625rem;
		color: rgb(156 163 175);
	}
	.stat-label {
		font-size: 0.625rem;
		color: rgb(156 163 175);
	}
	.stat-value {
		margin-top: 0.1rem;
		font-size: 0.9375rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		color: var(--app-fg);
	}
	.chip {
		border-radius: 9999px;
		padding: 0.05rem 0.45rem;
		font-weight: 500;
	}
	.link-btn {
		flex-shrink: 0;
		border-radius: 0.375rem;
		padding: 0.15rem 0.4rem;
		font-size: 0.6875rem;
		color: rgb(107 114 128);
		transition:
			background-color 0.1s,
			color 0.1s;
	}
	.link-btn:hover {
		color: var(--app-fg);
		background: color-mix(in oklab, var(--app-fg) 6%, transparent);
	}
</style>
