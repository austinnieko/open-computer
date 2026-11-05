<script lang="ts">
	/**
	 * One agent on the dashboard: a list row or a board card. Shows its state and
	 * why, runs its main action in place (Start / Stop / Resume / Retry), and
	 * expands to details plus a quick reply that goes straight into its chat.
	 */
	import { toast } from 'svelte-sonner';
	import { t } from '$lib/i18n';
	import { tooltip } from '$lib/tooltip';
	import Icon from '../Icon.svelte';
	import Spinner from '../common/Spinner.svelte';
	import { compactDuration, compactNumber } from '$lib/stores/coordination.svelte';
	import {
		folderName,
		INLINE_ACTIONS,
		MARK_DONE_FROM,
		markAgentDone,
		openAgentChat,
		openAgentsPanel,
		replyToAgent,
		runAgentAction,
		type AgentRow
	} from '$lib/stores/agents.svelte';
	import { ACTION_ICON, PILL, SWATCH, ago } from './ui';

	interface Props {
		row: AgentRow;
		variant?: 'row' | 'card';
		selected?: boolean;
		expanded?: boolean;
		/** Show the folder (board view, where rows aren't grouped by folder). */
		showFolder?: boolean;
		titles: Map<string, string>;
		dependsOn: string[];
		/** A dependency's task, by id (for its status and title). */
		taskOf: (id: string) => import('$lib/apis/coordination').CoordTask | undefined;
		now: number;
		ontoggle: () => void;
		onselect: () => void;
	}
	let {
		row,
		variant = 'row',
		selected = false,
		expanded = false,
		showFolder = false,
		titles,
		dependsOn,
		taskOf,
		now,
		ontoggle,
		onselect
	}: Props = $props();

	const task = $derived(row.task);
	const label = $derived(
		['review', 'draft', 'done', 'ended'].includes(row.state)
			? $t(`coordination.stage.${row.state}`)
			: $t(`coordination.activity.${row.state}`)
	);
	const detail = $derived.by(() => {
		const parts: string[] = [];
		if (row.life.detailKey && row.life.detailKey !== 'working') {
			parts.push($t(`coordination.detail.${row.life.detailKey}`, row.life.detailArgs ?? {}));
		}
		if (row.life.detailText) parts.push(row.life.detailText);
		return parts.join(' ');
	});
	const checkout = $derived(task.meta?.checkout as { kind?: string; note?: string } | undefined);
	const action = $derived(row.life.action);
	const canReply = $derived(!!task.chat_id && !task.chat_missing && row.state !== 'ended');
	const live = $derived(row.state === 'working');

	let busy = $state(false);
	async function act(e: Event) {
		e.stopPropagation();
		if (!action) return;
		if (action === 'reply') {
			if (!expanded) ontoggle();
			setTimeout(() => replyEl?.focus(), 30);
			return;
		}
		busy = true;
		try {
			await runAgentAction(row);
		} catch (err: any) {
			toast.error(err?.message ?? String(err));
		} finally {
			busy = false;
		}
	}

	let reply = $state('');
	let sending = $state(false);
	let replyEl = $state<HTMLTextAreaElement | null>(null);
	async function send() {
		const text = reply.trim();
		if (!text || sending) return;
		sending = true;
		try {
			await replyToAgent(task, text);
			reply = '';
			toast.success($t('agents.replySent', { title: task.title }));
		} catch (err: any) {
			toast.error(err?.message ?? String(err));
		} finally {
			sending = false;
		}
	}
	function replyKey(e: KeyboardEvent) {
		e.stopPropagation();
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			void send();
		} else if (e.key === 'Escape') {
			(e.currentTarget as HTMLElement).blur();
		}
	}

	const usage = $derived(row.usage);
	const goneDeps = $derived(
		dependsOn.map(taskOf).filter((d) => d && ['ended', 'failed'].includes(d.status))
	);
	let marking = $state<string | null>(null);
	async function markDone(target: import('$lib/apis/coordination').CoordTask) {
		marking = target.id;
		try {
			if (await markAgentDone(target, $t as (k: string, a?: Record<string, unknown>) => string))
				toast.success($t('coordination.markedDone'));
		} catch (err: any) {
			toast.error(err?.message ?? String(err));
		} finally {
			marking = null;
		}
	}
</script>

{#snippet actionButton(withLabel: boolean)}
	{#if action && action !== 'delete'}
		<button
			type="button"
			class="action-btn shrink-0 {INLINE_ACTIONS.has(action) || action === 'reply'
				? 'primary'
				: ''}"
			onclick={act}
			disabled={busy}
			use:tooltip={$t(`coordination.action.${action}`)}
			aria-label={$t(`coordination.action.${action}`)}
		>
			{#if busy}<Spinner size={11} />{:else}<Icon
					name={ACTION_ICON[action] ?? 'play'}
					size={11}
				/>{/if}
			{#if withLabel}<span class="hidden xl:inline">{$t(`coordination.action.${action}`)}</span
				>{/if}
		</button>
	{/if}
{/snippet}

<div
	class="agent-item group relative {variant === 'card'
		? 'rounded-xl border bg-white/60 shadow-[0_1px_0_rgba(0,0,0,0.02)] dark:bg-white/[0.025] ' +
			(selected
				? 'border-gray-300 ring-2 ring-blue-500/30 dark:border-white/20'
				: 'border-gray-200/80 hover:border-gray-300 dark:border-white/8 dark:hover:border-white/15')
		: selected
			? 'bg-blue-500/[0.06] dark:bg-blue-400/[0.07]'
			: 'hover:bg-gray-50 dark:hover:bg-white/[0.035]'} transition-colors duration-100"
	data-agent-id={task.id}
>
	{#if variant === 'row' && selected}
		<span class="absolute inset-y-1 left-0 w-0.5 rounded-full bg-blue-500"></span>
	{/if}
	<div
		class="flex cursor-pointer items-start gap-3 {variant === 'card'
			? 'p-3'
			: 'py-2.5 pr-3 pl-5 sm:pl-12'}"
		role="button"
		tabindex="-1"
		onclick={() => {
			onselect();
			ontoggle();
		}}
		onkeydown={(e) => e.key === 'Enter' && ontoggle()}
	>
		<!-- State dot (pulses while the agent works) -->
		<span class="relative flex h-5 w-2.5 shrink-0 items-center justify-center">
			{#if live}
				<span class="absolute inline-flex size-full animate-ping rounded-full bg-blue-500/60"
				></span>
			{/if}
			<span
				class="relative inline-flex size-2 rounded-full {SWATCH[row.state]} {row.state === 'draft'
					? 'outline-1 outline-slate-400 outline-dashed'
					: ''}"
			></span>
		</span>

		<div class="min-w-0 flex-1">
			<div class="flex h-5 min-w-0 items-center gap-2">
				<span
					class="truncate text-[0.8125rem] leading-5 font-medium text-gray-900 dark:text-gray-50"
					>{task.title}</span
				>
				{#if variant === 'row' && task.branch}
					<span class="hidden min-w-0 truncate font-mono text-[0.625rem] text-gray-400 md:inline"
						>{task.branch}</span
					>
				{/if}
			</div>
			{#if detail}
				<p
					class="mt-0.5 text-[0.6875rem] leading-snug text-gray-500 dark:text-gray-400 {expanded
						? ''
						: variant === 'card'
							? 'line-clamp-2'
							: 'truncate'}"
				>
					{detail}
				</p>
			{/if}
			{#if variant === 'card' || showFolder}
				<div
					class="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.625rem] text-gray-400"
				>
					<span class="inline-flex min-w-0 items-center gap-1 whitespace-nowrap">
						<Icon name={row.folder === row.repo ? 'folder' : 'git-branch'} size={10} />
						{folderName(row.repo)}{row.folder !== row.repo ? ` / ${folderName(row.folder)}` : ''}
					</span>
					{#if usage && usage.tokens}
						<span class="tabular-nums">{compactNumber(usage.tokens)} tok</span>
					{/if}
					<span class="tabular-nums">{ago(task.updated_at, now)}</span>
					{#if variant === 'card'}<span class="ml-auto">{@render actionButton(false)}</span>{/if}
				</div>
			{/if}
		</div>

		{#if variant === 'row'}
			<!-- Fixed-width columns so every row lines up, centred on the title line -->
			<div class="flex h-5 shrink-0 items-center gap-2">
				<span
					class="hidden w-24 truncate text-right text-[0.6875rem] tabular-nums text-gray-400 lg:block"
					>{usage
						? `${compactNumber(usage.tokens)} · ${compactDuration(usage.duration_ms)}`
						: ''}</span
				>
				<span class="hidden w-[5.75rem] justify-end sm:flex">
					<span
						class="rounded-full px-2 py-0.5 text-[0.6875rem] leading-4 font-medium whitespace-nowrap ring-1 ring-inset {PILL[
							row.state
						]}">{label}</span
					>
				</span>
				<span class="w-8 text-right text-[0.6875rem] tabular-nums text-gray-400"
					>{ago(task.updated_at, now)}</span
				>
				<span class="flex w-6 justify-center">{@render actionButton(false)}</span>
				<Icon
					name="chevron-down"
					size={12}
					class="text-gray-400 transition-transform duration-150 {expanded ? 'rotate-180' : ''}"
				/>
			</div>
		{:else}
			<Icon
				name="chevron-down"
				size={12}
				class="mt-1 shrink-0 text-gray-400 transition-transform duration-150 {expanded
					? 'rotate-180'
					: ''}"
			/>
		{/if}
	</div>

	{#if expanded}
		<div class="expand space-y-3 pb-3 {variant === 'card' ? 'px-3' : 'pr-3 pl-5 sm:pl-[3.75rem]'}">
			{#if variant === 'card'}
				<span
					class="inline-block rounded-full px-2 py-0.5 text-[0.6875rem] font-medium ring-1 ring-inset {PILL[
						row.state
					]}">{label}</span
				>
			{/if}
			{#if task.description}
				<p class="max-w-prose text-xs leading-relaxed text-gray-600 dark:text-gray-300">
					{task.description}
				</p>
			{/if}
			{#if checkout?.note}
				<div
					class="rounded-lg border border-gray-200/70 bg-gray-50/70 px-2.5 py-1.5 text-xs text-gray-600 dark:border-white/8 dark:bg-white/[0.03] dark:text-gray-300"
				>
					<span class="mr-1 font-mono text-[0.625rem] uppercase tracking-wide text-gray-400"
						>{$t('agents.lastCheckout')} · {checkout.kind}</span
					>
					{checkout.note}
				</div>
			{/if}

			<dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-[0.6875rem] sm:grid-cols-4">
				{#if usage}
					<div>
						<dt class="text-gray-400">{$t('agents.tokens')}</dt>
						<dd class="tabular-nums text-gray-700 dark:text-gray-200">
							{compactNumber(usage.tokens)}
						</dd>
					</div>
					<div>
						<dt class="text-gray-400">{$t('agents.agentTime')}</dt>
						<dd class="tabular-nums text-gray-700 dark:text-gray-200">
							{compactDuration(usage.duration_ms)}
						</dd>
					</div>
					<div>
						<dt class="text-gray-400">{$t('agents.turnsTools')}</dt>
						<dd class="tabular-nums text-gray-700 dark:text-gray-200">
							{usage.turns} · {usage.tool_calls}
						</dd>
					</div>
					<div>
						<dt class="text-gray-400">{$t('agents.files')}</dt>
						<dd class="tabular-nums text-gray-700 dark:text-gray-200">{usage.files}</dd>
					</div>
				{/if}
				{#if task.branch}
					<div class="col-span-2">
						<dt class="text-gray-400">{$t('agents.branch')}</dt>
						<dd class="truncate font-mono text-gray-700 dark:text-gray-200">{task.branch}</dd>
					</div>
				{/if}
				<div class="col-span-2">
					<dt class="text-gray-400">{$t('agents.folder')}</dt>
					<dd class="truncate text-gray-700 dark:text-gray-200" use:tooltip={row.folder}>
						{folderName(row.folder)}
						<span class="text-gray-400"
							>· {row.folder === row.repo ? $t('agents.mainCheckout') : $t('agents.worktree')}</span
						>
					</dd>
				</div>
				{#if dependsOn.length}
					<div class="col-span-2 sm:col-span-4">
						<dt class="text-gray-400">{$t('agents.dependsOn')}</dt>
						<dd class="flex flex-wrap gap-1 pt-0.5">
							{#each dependsOn as id (id)}
								<span
									class="rounded-md bg-gray-100 px-1.5 py-0.5 text-gray-600 dark:bg-white/8 dark:text-gray-300"
									>{titles.get(id) ?? id}</span
								>
							{/each}
						</dd>
					</div>
				{/if}
			</dl>

			{#if canReply}
				<div
					class="reply flex items-end gap-2 rounded-xl border border-gray-200 bg-white px-2.5 py-1.5 focus-within:border-blue-400/60 focus-within:ring-2 focus-within:ring-blue-500/15 dark:border-white/10 dark:bg-white/[0.03]"
				>
					<textarea
						bind:this={replyEl}
						bind:value={reply}
						rows="1"
						class="max-h-32 min-h-[1.5rem] flex-1 resize-none bg-transparent py-0.5 text-xs leading-relaxed outline-none placeholder:text-gray-400"
						placeholder={row.state === 'needs_you'
							? $t('agents.replyPlaceholderNeeds')
							: $t('agents.replyPlaceholder')}
						onkeydown={replyKey}
						onclick={(e) => e.stopPropagation()}
					></textarea>
					<button
						type="button"
						class="flex size-6 shrink-0 items-center justify-center rounded-lg bg-gray-900 text-white transition-opacity disabled:opacity-30 dark:bg-white dark:text-gray-900"
						disabled={!reply.trim() || sending}
						onclick={(e) => {
							e.stopPropagation();
							void send();
						}}
						aria-label={$t('agents.send')}
					>
						{#if sending}<Spinner size={10} />{:else}<Icon
								name="arrow-right-from-line"
								size={11}
							/>{/if}
					</button>
				</div>
			{/if}

			<div class="flex flex-wrap gap-1.5">
				{#if task.chat_id && !task.chat_missing}
					<button
						class="ghost-btn"
						onclick={(e) => {
							e.stopPropagation();
							openAgentChat(task, row.folder);
						}}><Icon name="chat-bubble" size={12} />{$t('agents.openChat')}</button
					>
				{/if}
				<button
					class="ghost-btn"
					onclick={(e) => {
						e.stopPropagation();
						openAgentsPanel(row.folder, task.id);
					}}><Icon name="info" size={12} />{$t('agents.details')}</button
				>
				{#each goneDeps as dep (dep!.id)}
					<button
						class="ghost-btn emphasis"
						disabled={marking === dep!.id}
						onclick={(e) => {
							e.stopPropagation();
							void markDone(dep!);
						}}
						use:tooltip={$t('coordination.markDepDoneHint')}
						><Icon name="check" size={12} />{$t('coordination.markDepDone', {
							title: dep!.title
						})}</button
					>
				{/each}
				{#if MARK_DONE_FROM.has(task.status) && !task.chat_missing}
					<button
						class="ghost-btn"
						disabled={marking === task.id}
						onclick={(e) => {
							e.stopPropagation();
							void markDone(task);
						}}
						use:tooltip={$t('coordination.markDoneHint')}
						><Icon name="check" size={12} />{task.status === 'ended'
							? $t('coordination.markDoneInstead')
							: $t('coordination.markDone')}</button
					>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.action-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.3rem;
		width: 1.5rem;
		height: 1.5rem;
		padding: 0;
		border-radius: 0.5rem;
		font-size: 0.6875rem;
		font-weight: 500;
		color: rgb(107 114 128);
		border: 1px solid color-mix(in oklab, var(--app-fg) 12%, transparent);
		transition:
			background-color 0.1s,
			color 0.1s,
			border-color 0.1s;
	}
	.action-btn:hover {
		color: var(--app-fg);
		background: color-mix(in oklab, var(--app-fg) 6%, transparent);
	}
	.action-btn.primary {
		color: var(--app-fg);
		border-color: color-mix(in oklab, var(--app-fg) 18%, transparent);
	}
	.action-btn:disabled {
		opacity: 0.5;
	}
	.ghost-btn {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		height: 1.625rem;
		padding: 0 0.6rem;
		border-radius: 0.5rem;
		font-size: 0.6875rem;
		color: rgb(107 114 128);
		background: color-mix(in oklab, var(--app-fg) 5%, transparent);
		transition:
			background-color 0.1s,
			color 0.1s;
	}
	.ghost-btn:hover {
		color: var(--app-fg);
		background: color-mix(in oklab, var(--app-fg) 9%, transparent);
	}
	.ghost-btn.emphasis {
		color: rgb(180 83 9);
		background: rgb(245 158 11 / 0.14);
	}
	:global(.dark) .ghost-btn.emphasis {
		color: rgb(252 211 77);
	}
	.ghost-btn:disabled {
		opacity: 0.5;
	}
	.expand {
		animation: expand-in 0.14s ease-out;
	}
	@keyframes expand-in {
		from {
			opacity: 0;
			transform: translateY(-2px);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
</style>
