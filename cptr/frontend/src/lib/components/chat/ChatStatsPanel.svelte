<script lang="ts">
	/**
	 * Usage for the open chat, shown in the space beside the conversation:
	 * tokens and cost, context fill, time working, per-reply activity, tools,
	 * files touched and sub-agents. Refreshes while the chat is running.
	 */
	import { getChatStats, type ChatStats } from '$lib/apis/chat';
	import { openFileTab } from '$lib/stores';
	import { untrack } from 'svelte';
	import Icon from '../Icon.svelte';

	interface Props {
		chatId: string;
		workspace?: string | null;
		running?: boolean;
		onclose?: () => void;
	}

	let { chatId, workspace = null, running = false, onclose }: Props = $props();

	let stats = $state<ChatStats | null>(null);
	let failed = $state(false);
	let hoverTurn = $state<number | null>(null);

	async function load(id: string) {
		try {
			const next = await getChatStats(id);
			if (id === chatId) {
				stats = next;
				failed = false;
			}
		} catch {
			if (id === chatId) failed = true;
		}
	}

	// Load on chat change; poll while it runs, and once more when it stops.
	$effect(() => {
		const id = chatId;
		const live = running;
		if (!id || id.startsWith('new-') || id.startsWith('pending-')) {
			stats = null;
			return;
		}
		untrack(() => load(id));
		if (!live) return;
		const timer = setInterval(() => load(id), 3000);
		return () => {
			clearInterval(timer);
			untrack(() => load(id));
		};
	});

	const compact = (n: number) =>
		new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n);
	const full = (n: number) => new Intl.NumberFormat().format(n);
	function duration(ms: number): string {
		if (!ms) return '—';
		const s = Math.round(ms / 1000);
		if (s < 60) return `${s}s`;
		const m = Math.floor(s / 60);
		if (m < 60) return `${m}m ${s % 60}s`;
		return `${Math.floor(m / 60)}h ${m % 60}m`;
	}
	function money(usd: number): string {
		if (!usd) return '';
		return usd < 0.01 ? '<$0.01' : `$${usd.toFixed(usd < 1 ? 3 : 2)}`;
	}
	function shortModel(id: string): string {
		return (
			id
				.replace(/^agent:/, '')
				.split('/')
				.slice(-1)[0] || id
		);
	}

	const totals = $derived(stats?.totals);
	const allTokens = $derived((totals?.total ?? 0) + (stats?.subagents.tokens ?? 0));
	const allCost = $derived((totals?.cost_usd ?? 0) + (stats?.subagents.cost_usd ?? 0));
	const toolCalls = $derived((stats?.tools ?? []).reduce((n, t) => n + t.count, 0));
	const failedCalls = $derived((stats?.tools ?? []).reduce((n, t) => n + t.failed, 0));
	const ctx = $derived(stats?.context_usage ?? null);
	const ctxPercent = $derived(Math.max(0, Math.min(100, ctx?.percent ?? 0)));
	const ringR = 30;
	const ringC = 2 * Math.PI * ringR;

	const mix = $derived.by(() => {
		const input = totals?.input ?? 0;
		const output = totals?.output ?? 0;
		const sum = input + output;
		return sum ? { input, output, inPct: (input / sum) * 100 } : null;
	});

	const turns = $derived(stats?.turns ?? []);

	// While a reply runs, count its time up live from when it started.
	let now = $state(Date.now());
	$effect(() => {
		if (!running) return;
		const tick = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(tick);
	});
	const liveTurn = $derived(running ? turns.findLast((t) => !t.done) : undefined);
	const workingMs = $derived(
		(totals?.duration_ms ?? 0) +
			(liveTurn
				? Math.max(
						0,
						now - (liveTurn.created_at > 1e12 ? liveTurn.created_at : liveTurn.created_at * 1000)
					)
				: 0)
	);
	const turnMax = $derived(Math.max(1, ...turns.map((t) => t.tokens || t.tool_calls)));
	const useTokens = $derived(turns.some((t) => t.tokens > 0));
	const topTools = $derived((stats?.tools ?? []).slice(0, 6));
	const toolMax = $derived(Math.max(1, ...topTools.map((t) => t.count)));
	const files = $derived((stats?.files ?? []).slice(0, 6));

	function openFile(path: string) {
		const abs =
			path.startsWith('/') || !workspace ? path : `${workspace.replace(/\/$/, '')}/${path}`;
		openFileTab(abs);
	}
</script>

<aside class="stats" aria-label="Chat stats">
	<header class="stats-head">
		<span class="stats-title">
			{#if running}<span class="live-dot" aria-hidden="true"></span>{/if}
			This chat
		</span>
		{#if onclose}
			<button type="button" class="icon-btn" onclick={onclose} aria-label="Hide chat stats">
				<Icon name="xmark" size={12} />
			</button>
		{/if}
	</header>

	{#if !stats}
		<div class="empty">{failed ? 'Stats unavailable.' : 'Loading…'}</div>
	{:else}
		<!-- Hero: tokens + cost -->
		<section class="card hero">
			<div class="hero-num" title={`${full(allTokens)} tokens`}>
				{allTokens ? compact(allTokens) : '—'}
			</div>
			<div class="muted small">
				tokens{#if allCost}&nbsp;·&nbsp;<span class="ink">{money(allCost)}</span>{/if}
				{#if stats.subagents.tokens}
					<span class="block">incl. {compact(stats.subagents.tokens)} from sub-agents</span>
				{/if}
			</div>
			{#if mix}
				<div
					class="mix"
					role="img"
					aria-label={`Input ${full(mix.input)}, output ${full(mix.output)} tokens`}
				>
					<span class="mix-seg seg-in" style="width: {mix.inPct}%"></span>
					<span class="mix-seg seg-out" style="width: {100 - mix.inPct}%"></span>
				</div>
				<div class="legend">
					<span><i class="key seg-in"></i>Input <b>{compact(mix.input)}</b></span>
					<span><i class="key seg-out"></i>Output <b>{compact(mix.output)}</b></span>
				</div>
				{#if totals && (totals.cached || totals.reasoning)}
					<div class="muted small extra">
						{#if totals.cached}Cached {compact(totals.cached)}{/if}
						{#if totals.cached && totals.reasoning}&nbsp;·&nbsp;{/if}
						{#if totals.reasoning}Reasoning {compact(totals.reasoning)}{/if}
					</div>
				{/if}
			{:else if !allTokens}
				<div class="muted small extra">
					{running ? 'Counted when the reply finishes.' : "This model didn't report token counts."}
				</div>
			{/if}
		</section>

		<!-- Context ring -->
		{#if ctx && ctx.threshold}
			<section class="card ctx">
				<svg
					width="76"
					height="76"
					viewBox="0 0 76 76"
					role="img"
					aria-label={`Context ${Math.round(ctxPercent)}% full`}
				>
					<circle cx="38" cy="38" r={ringR} class="ring-track" />
					<circle
						cx="38"
						cy="38"
						r={ringR}
						class="ring-fill {ctxPercent >= 85 ? 'ring-hot' : ''}"
						stroke-dasharray={`${(ctxPercent / 100) * ringC} ${ringC}`}
						transform="rotate(-90 38 38)"
					/>
					<text x="38" y="42" text-anchor="middle" class="ring-label"
						>{Math.round(ctxPercent)}%</text
					>
				</svg>
				<div class="ctx-text">
					<div class="label">Context</div>
					<div class="ink">
						{compact(ctx.tokens || ctx.estimated_tokens)}
						<span class="muted">/ {compact(ctx.threshold)}</span>
					</div>
					<div class="muted small">
						{ctxPercent >= 85 ? 'Near the compaction point' : 'until it compacts'}
					</div>
				</div>
			</section>
		{/if}

		<!-- Tiles -->
		<section class="tiles">
			<div class="tile">
				<div class="label">Working</div>
				<div class="tile-num">{duration(workingMs)}</div>
			</div>
			<div class="tile">
				<div class="label">Replies</div>
				<div class="tile-num">{turns.length}</div>
			</div>
			<div class="tile">
				<div class="label">Tool calls</div>
				<div class="tile-num">
					{toolCalls + stats.subagents.tool_calls}
					{#if failedCalls}<span class="fail-tag" title="Failed calls">{failedCalls} failed</span
						>{/if}
				</div>
			</div>
			<div class="tile">
				<div class="label">Files</div>
				<div class="tile-num">{stats.files.length}</div>
			</div>
		</section>

		<!-- Per-reply activity -->
		{#if turns.length > 1}
			<section class="card">
				<div class="row-head">
					<span class="label">{useTokens ? 'Tokens per reply' : 'Tool calls per reply'}</span>
					<span class="muted small">
						{#if hoverTurn !== null}
							#{hoverTurn + 1}: {useTokens
								? `${full(turns[hoverTurn].tokens)} tokens`
								: `${turns[hoverTurn].tool_calls} calls`}{turns[hoverTurn].duration_ms
								? ` · ${duration(turns[hoverTurn].duration_ms)}`
								: ''}
						{:else}
							{turns.length} replies
						{/if}
					</span>
				</div>
				<div class="spark" role="list" onmouseleave={() => (hoverTurn = null)}>
					{#each turns as turn, i (turn.id)}
						{@const v = useTokens ? turn.tokens : turn.tool_calls}
						<div
							class="spark-hit"
							role="listitem"
							onmouseenter={() => (hoverTurn = i)}
							aria-label={`Reply ${i + 1}: ${v}`}
						>
							<span
								class="spark-bar {hoverTurn === i ? 'is-hover' : ''} {turn.done ? '' : 'is-live'}"
								style="height: {Math.max(4, (v / turnMax) * 100)}%"
							></span>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- Tools -->
		{#if topTools.length}
			<section class="card">
				<div class="label">Top tools</div>
				<div class="bars">
					{#each topTools as tool (tool.name)}
						<div
							class="bar-row"
							title={`${tool.count} calls${tool.failed ? `, ${tool.failed} failed` : ''}`}
						>
							<span class="bar-name">{tool.name}</span>
							<span class="bar-track">
								<span class="bar-fill" style="width: {(tool.count / toolMax) * 100}%"></span>
							</span>
							<span class="bar-val">{tool.count}</span>
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- Files -->
		{#if files.length}
			<section class="card">
				<div class="label">Files touched</div>
				<ul class="files">
					{#each files as file (file.path)}
						<li>
							<button
								type="button"
								class="file"
								onclick={() => openFile(file.path)}
								title={file.path}
							>
								<span class="file-name">{file.path.split('/').pop()}</span>
								<span class="file-dir"
									>{file.path.includes('/')
										? file.path.slice(0, file.path.lastIndexOf('/'))
										: ''}</span
								>
								<span class="file-count">{file.edits}×</span>
							</button>
						</li>
					{/each}
				</ul>
				{#if stats.files.length > files.length}
					<div class="muted small">+{stats.files.length - files.length} more</div>
				{/if}
			</section>
		{/if}

		<!-- Sub-agents + models -->
		{#if stats.subagents.count || stats.models.length}
			<section class="card">
				{#if stats.subagents.count}
					<div class="row-head">
						<span class="label">Sub-agents</span>
						<span class="ink small">
							{stats.subagents.count}{#if stats.subagents.running}
								<span class="muted"> · {stats.subagents.running} working</span>{/if}
						</span>
					</div>
				{/if}
				{#if stats.models.length}
					<div class="chips">
						{#each stats.models as model (model.id)}
							<span class="chip" title={model.id}>{shortModel(model.id)}<b>{model.replies}</b></span
							>
						{/each}
					</div>
				{/if}
			</section>
		{/if}
	{/if}
</aside>

<style>
	.stats {
		--s-in: #2a78d6;
		--s-out: #eb6834;
		--s-track: color-mix(in oklab, var(--app-fg) 8%, transparent);
		--s-card: color-mix(in oklab, var(--app-fg) 3%, var(--app-bg));
		--s-border: color-mix(in oklab, var(--app-fg) 8%, transparent);
		--s-muted: color-mix(in oklab, var(--app-fg) 50%, transparent);
		--s-hot: #e34948;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		font-size: 0.75rem;
		color: var(--app-fg);
	}
	:global(.dark) .stats {
		--s-in: #3987e5;
		--s-out: #d95926;
		--s-hot: #e66767;
	}

	.stats-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0 0.125rem;
	}
	.stats-title {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.6875rem;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--s-muted);
	}
	.live-dot {
		width: 6px;
		height: 6px;
		border-radius: 999px;
		background: var(--s-in);
		box-shadow: 0 0 0 0 color-mix(in oklab, var(--s-in) 60%, transparent);
		animation: live 1.6s ease-out infinite;
	}
	@keyframes live {
		70% {
			box-shadow: 0 0 0 6px transparent;
		}
		100% {
			box-shadow: 0 0 0 0 transparent;
		}
	}
	.icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.25rem;
		height: 1.25rem;
		border-radius: 0.375rem;
		color: var(--s-muted);
	}
	.icon-btn:hover {
		background: var(--s-track);
		color: var(--app-fg);
	}

	.card {
		background: var(--s-card);
		border: 1px solid var(--s-border);
		border-radius: 0.875rem;
		padding: 0.75rem 0.875rem;
	}
	.label {
		font-size: 0.625rem;
		font-weight: 600;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--s-muted);
	}
	.muted {
		color: var(--s-muted);
	}
	.ink {
		color: var(--app-fg);
	}
	.small {
		font-size: 0.6875rem;
	}
	.thin {
		font-weight: 400;
		font-size: 0.75rem;
	}
	.empty {
		padding: 1rem 0.25rem;
		color: var(--s-muted);
	}

	.hero-num {
		font-size: 2rem;
		line-height: 1.05;
		font-weight: 650;
		letter-spacing: -0.03em;
		font-variant-numeric: tabular-nums;
		background: linear-gradient(120deg, var(--app-fg) 40%, var(--s-in));
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	.block {
		display: block;
	}
	.mix {
		display: flex;
		gap: 2px;
		height: 6px;
		margin-top: 0.625rem;
	}
	.mix-seg {
		display: block;
		height: 100%;
		min-width: 4px;
		border-radius: 4px;
		transition: width 0.4s ease;
	}
	.seg-in {
		background: var(--s-in);
	}
	.seg-out {
		background: var(--s-out);
	}
	.legend {
		display: flex;
		gap: 0.75rem;
		margin-top: 0.375rem;
		font-size: 0.6875rem;
		color: var(--s-muted);
	}
	.legend b {
		color: var(--app-fg);
		font-weight: 600;
		margin-left: 0.125rem;
	}
	.key {
		display: inline-block;
		width: 8px;
		height: 8px;
		border-radius: 2px;
		margin-right: 0.3rem;
		vertical-align: -0.5px;
	}
	.extra {
		margin-top: 0.25rem;
	}

	.ctx {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.5rem 0.875rem 0.5rem 0.5rem;
	}
	.ring-track {
		fill: none;
		stroke: var(--s-track);
		stroke-width: 6;
	}
	.ring-fill {
		fill: none;
		stroke: var(--s-in);
		stroke-width: 6;
		stroke-linecap: round;
		transition: stroke-dasharray 0.5s ease;
	}
	.ring-hot {
		stroke: var(--s-hot);
	}
	.ring-label {
		fill: var(--app-fg);
		font-size: 13px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.ctx-text {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
		font-variant-numeric: tabular-nums;
	}

	.tiles {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.5rem;
	}
	.tile {
		background: var(--s-card);
		border: 1px solid var(--s-border);
		border-radius: 0.875rem;
		padding: 0.625rem 0.75rem;
	}
	.tile-num {
		margin-top: 0.125rem;
		font-size: 1.125rem;
		font-weight: 600;
		letter-spacing: -0.01em;
		font-variant-numeric: tabular-nums;
		display: flex;
		align-items: baseline;
		gap: 0.25rem;
		flex-wrap: wrap;
	}
	.fail-tag {
		font-size: 0.625rem;
		font-weight: 500;
		color: var(--s-hot);
	}

	.row-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.spark {
		display: flex;
		align-items: flex-end;
		gap: 2px;
		height: 44px;
		margin-top: 0.5rem;
	}
	.spark-hit {
		flex: 1;
		height: 100%;
		display: flex;
		align-items: flex-end;
		cursor: default;
	}
	.spark-bar {
		display: block;
		width: 100%;
		border-radius: 4px 4px 0 0;
		background: color-mix(in oklab, var(--s-in) 55%, transparent);
		transition:
			height 0.4s ease,
			background 0.15s;
	}
	.spark-bar.is-hover {
		background: var(--s-in);
	}
	.spark-bar.is-live {
		background: var(--s-in);
		animation: pulse 1.2s ease-in-out infinite;
	}
	@keyframes pulse {
		50% {
			opacity: 0.45;
		}
	}

	.bars {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		margin-top: 0.5rem;
	}
	.bar-row {
		display: grid;
		grid-template-columns: minmax(0, 5.5rem) 1fr auto;
		align-items: center;
		gap: 0.5rem;
	}
	.bar-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--app-fg);
		font-size: 0.6875rem;
	}
	.bar-track {
		height: 6px;
		border-radius: 4px;
		background: var(--s-track);
		overflow: hidden;
	}
	.bar-fill {
		display: block;
		height: 100%;
		border-radius: 4px;
		background: var(--s-in);
		transition: width 0.4s ease;
	}
	.bar-val {
		font-variant-numeric: tabular-nums;
		color: var(--s-muted);
		font-size: 0.6875rem;
		min-width: 1.25rem;
		text-align: right;
	}

	.files {
		margin: 0.375rem 0 0;
		padding: 0;
		list-style: none;
	}
	.file {
		display: flex;
		align-items: baseline;
		gap: 0.375rem;
		width: 100%;
		padding: 0.25rem 0.375rem;
		margin: 0 -0.375rem;
		border-radius: 0.375rem;
		text-align: left;
	}
	.file:hover {
		background: var(--s-track);
	}
	.file-name {
		font-weight: 500;
		white-space: nowrap;
	}
	.file-dir {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--s-muted);
		font-size: 0.6875rem;
	}
	.file-count {
		color: var(--s-muted);
		font-variant-numeric: tabular-nums;
		font-size: 0.6875rem;
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
		margin-top: 0.5rem;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		max-width: 100%;
		padding: 0.125rem 0.5rem;
		border-radius: 999px;
		border: 1px solid var(--s-border);
		font-size: 0.6875rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.chip b {
		font-weight: 600;
		color: var(--s-muted);
		font-variant-numeric: tabular-nums;
	}
</style>
