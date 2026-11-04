<script lang="ts">
	/**
	 * Every sub-agent this chat delegated to: a live list, and a timeline of
	 * the handoffs between the main agent and each sub-agent.
	 */
	import Icon from '../Icon.svelte';
	import type { SubagentInfo, SubagentStatus } from '$lib/apis/chat';
	import { describeActivity, formatElapsed, subagentsOf, toMs } from '$lib/stores/subagents.svelte';

	interface Props {
		chatId: string | null;
	}

	let { chatId }: Props = $props();

	const agents = $derived(subagentsOf(chatId));
	const runningCount = $derived(
		agents.filter((a) => a.status === 'running' || a.status === 'queued').length
	);
	const doneCount = $derived(agents.filter((a) => a.status === 'done').length);
	const failedCount = $derived(
		agents.filter((a) => a.status === 'failed' || a.status === 'stopped').length
	);

	let open = $state(false);
	let view = $state<'list' | 'timeline'>('list');
	// Open on its own the first time something starts working.
	let autoOpened = false;
	$effect(() => {
		if (runningCount > 0 && !autoOpened) {
			autoOpened = true;
			open = true;
		}
	});

	let now = $state(Date.now());
	$effect(() => {
		if (!open || runningCount === 0) return;
		const id = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(id);
	});

	const isLive = (a: SubagentInfo) => a.status === 'running' || a.status === 'queued';
	const startOf = (a: SubagentInfo) => toMs(a.started_at);
	const endOf = (a: SubagentInfo) => (isLive(a) ? now : Math.max(toMs(a.updated_at), startOf(a)));

	const range = $derived.by(() => {
		if (!agents.length) return { start: 0, span: 1 };
		const start = Math.min(...agents.map(startOf));
		const end = Math.max(...agents.map(endOf), start + 1000);
		const pad = (end - start) * 0.03;
		return { start: start - pad, span: end - start + pad * 2 };
	});
	const pct = (t: number) => ((t - range.start) / range.span) * 100;

	const statusDot: Record<SubagentStatus, string> = {
		queued: 'bg-gray-400',
		running: 'bg-blue-500 animate-pulse',
		done: 'bg-emerald-500',
		failed: 'bg-red-500',
		stopped: 'bg-amber-500'
	};
	const barColor: Record<SubagentStatus, string> = {
		queued: 'bg-gray-400/40',
		running: 'bg-blue-500/60',
		done: 'bg-emerald-500/60',
		failed: 'bg-red-500/60',
		stopped: 'bg-amber-500/60'
	};

	function jumpTo(a: SubagentInfo) {
		if (!a.call_id) return;
		const el = document.getElementById(`subagent-${a.call_id}`);
		el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
		el?.classList.add('ring-2', 'ring-blue-400/60');
		setTimeout(() => el?.classList.remove('ring-2', 'ring-blue-400/60'), 1500);
	}

	const clock = (t: number) =>
		new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
	const short = (text: string, n = 90) => {
		const one = text.replace(/\s+/g, ' ').trim();
		return one.length > n ? `${one.slice(0, n)}…` : one;
	};

	// Chronological log of messages between the main agent and sub-agents.
	const exchanges = $derived.by(() => {
		const rows: { at: number; from: string; to: string; text: string; agent: SubagentInfo }[] = [];
		agents.forEach((a, i) => {
			const label = `Agent ${i + 1}`;
			rows.push({ at: startOf(a), from: 'Main', to: label, text: a.task, agent: a });
			if (!isLive(a)) {
				const reply =
					a.status === 'done' ? a.result || 'Finished with no reply.' : `Ended: ${a.status}.`;
				rows.push({ at: endOf(a), from: label, to: 'Main', text: reply, agent: a });
			}
		});
		return rows.sort((x, y) => x.at - y.at);
	});

	const LANE = 30;
</script>

{#if agents.length > 0}
	<div class="app-subtle-surface my-1 overflow-hidden rounded-3xl border shadow-sm">
		<div class="flex items-center gap-2 px-3.5 pt-1.5 pb-1">
			<button
				type="button"
				class="app-muted flex min-w-0 flex-1 items-center gap-2 text-left text-xs"
				onclick={() => (open = !open)}
				aria-expanded={open}
			>
				<Icon name="user" size={14} />
				<span class="font-medium text-gray-700 dark:text-gray-300">Agents</span>
				{#if runningCount}
					<span class="flex items-center gap-1 text-blue-600 dark:text-blue-400">
						<span class="size-1.5 animate-pulse rounded-full bg-current"></span>
						{runningCount} working
					</span>
				{/if}
				{#if doneCount}<span>{doneCount} done</span>{/if}
				{#if failedCount}<span class="text-red-600 dark:text-red-400">{failedCount} failed</span
					>{/if}
			</button>
			{#if open}
				<div class="flex rounded-full border p-0.5 text-[11px]">
					{#each [['list', 'List'], ['timeline', 'Timeline']] as [key, label] (key)}
						<button
							type="button"
							class="rounded-full px-2 py-0.5 transition-colors {view === key
								? 'bg-gray-200 text-gray-800 dark:bg-gray-700 dark:text-gray-100'
								: 'app-muted hover:text-gray-700 dark:hover:text-gray-300'}"
							onclick={() => (view = key as 'list' | 'timeline')}
						>
							{label}
						</button>
					{/each}
				</div>
			{/if}
			<button
				type="button"
				class="app-muted flex size-6 items-center justify-center rounded-full hover:text-gray-600 dark:hover:text-gray-300"
				onclick={() => (open = !open)}
				aria-label={open ? 'Collapse agents' : 'Expand agents'}
			>
				<Icon name={open ? 'chevron-down' : 'chevron-up'} size={12} />
			</button>
		</div>

		{#if open && view === 'list'}
			<div class="max-h-60 space-y-0.5 overflow-y-auto px-2 pb-2.5">
				{#each agents as agent, i (agent.chat_id)}
					<button
						type="button"
						class="flex w-full items-start gap-2 rounded-2xl px-1.5 py-1 text-left text-xs hover:bg-gray-500/5"
						onclick={() => jumpTo(agent)}
					>
						<span class="mt-1 size-2 shrink-0 rounded-full {statusDot[agent.status]}"></span>
						<span class="min-w-0 flex-1">
							<span class="flex items-center gap-1.5">
								<span class="app-muted shrink-0">Agent {i + 1}</span>
								{#if agent.mode === 'background'}
									<span class="app-muted shrink-0 rounded border px-1 text-[10px]">background</span>
								{/if}
								<span class="truncate text-gray-800 dark:text-gray-200"
									>{short(agent.task, 120)}</span
								>
							</span>
							<span class="app-muted block truncate">{describeActivity(agent)}</span>
						</span>
						<span class="app-muted shrink-0 tabular-nums">
							{formatElapsed(endOf(agent) - startOf(agent))}
						</span>
					</button>
				{/each}
			</div>
		{/if}

		{#if open && view === 'timeline'}
			<div class="px-3.5 pb-3">
				<!-- Swimlanes: main agent on top, one lane per sub-agent -->
				<div class="relative text-[11px]" style="height: {(agents.length + 1) * LANE}px">
					<div class="absolute inset-y-0 left-0 w-20">
						<div class="app-muted flex items-center font-medium" style="height: {LANE}px">Main</div>
						{#each agents as agent, i (agent.chat_id)}
							<button
								type="button"
								class="app-muted flex w-full items-center truncate text-left hover:text-gray-700 dark:hover:text-gray-300"
								style="height: {LANE}px"
								title={agent.task}
								onclick={() => jumpTo(agent)}
							>
								Agent {i + 1}
							</button>
						{/each}
					</div>
					<div class="absolute inset-y-0 right-0 left-20">
						<!-- Main lane -->
						<div
							class="absolute inset-x-0 rounded-full bg-gray-400/25"
							style="top: {LANE / 2 - 2}px; height: 4px"
						></div>
						{#each agents as agent, i (agent.chat_id)}
							{@const top = (i + 1) * LANE}
							{@const left = pct(startOf(agent))}
							{@const right = pct(endOf(agent))}
							<!-- Handoff: main → agent -->
							<div
								class="absolute border-l border-dashed border-gray-400/70"
								style="left: {left}%; top: {LANE / 2}px; height: {top}px"
								title={`Main → Agent ${i + 1} at ${clock(startOf(agent))}: ${short(agent.task, 200)}`}
							></div>
							<div
								class="absolute size-2 -translate-x-1/2 rounded-full bg-gray-500"
								style="left: {left}%; top: {LANE / 2 - 4}px"
							></div>
							<!-- Agent's working span -->
							<button
								type="button"
								class="absolute rounded-full {barColor[agent.status]} hover:opacity-80"
								style="left: {left}%; width: max(6px, {right - left}%); top: {top +
									LANE / 2 -
									5}px; height: 10px"
								title={`Agent ${i + 1}: ${short(agent.task, 200)}\n${describeActivity(agent)} · ${formatElapsed(endOf(agent) - startOf(agent))}`}
								aria-label={`Agent ${i + 1} timeline`}
								onclick={() => jumpTo(agent)}
							></button>
							<!-- Result: agent → main -->
							{#if !isLive(agent)}
								<div
									class="absolute border-l border-dashed {agent.status === 'done'
										? 'border-emerald-500/70'
										: 'border-red-500/70'}"
									style="left: {right}%; top: {LANE / 2}px; height: {top}px"
									title={`Agent ${i + 1} → Main at ${clock(endOf(agent))}: ${short(agent.result || agent.status, 200)}`}
								></div>
								<div
									class="absolute size-2 -translate-x-1/2 rounded-full {agent.status === 'done'
										? 'bg-emerald-500'
										: 'bg-red-500'}"
									style="left: {right}%; top: {LANE / 2 - 4}px"
								></div>
							{/if}
						{/each}
					</div>
				</div>
				<div class="app-muted mt-1 flex justify-between pl-20 text-[10px] tabular-nums">
					<span>{clock(range.start)}</span>
					<span>{clock(range.start + range.span)}</span>
				</div>

				<!-- Message log -->
				<div class="mt-2 max-h-48 space-y-1 overflow-y-auto border-t pt-2">
					{#each exchanges as row, idx (idx)}
						<button
							type="button"
							class="flex w-full items-baseline gap-2 rounded-lg px-1 py-0.5 text-left text-xs hover:bg-gray-500/5"
							onclick={() => jumpTo(row.agent)}
						>
							<span class="app-muted shrink-0 tabular-nums">{clock(row.at)}</span>
							<span class="shrink-0 font-medium text-gray-700 dark:text-gray-300">
								{row.from} → {row.to}
							</span>
							<span class="app-muted truncate">{short(row.text, 160)}</span>
						</button>
					{/each}
				</div>
			</div>
		{/if}
	</div>
{/if}
