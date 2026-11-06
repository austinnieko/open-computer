<script lang="ts">
	/**
	 * Live progress for the reply that is still running, above the message box:
	 * what the agent is doing now, what it has done, and whether it needs you.
	 *
	 * Built only from the reply's real steps (the same items the chat shows):
	 * the agent's own todo list when it keeps one (OpenCode's todowrite), else
	 * its latest steps grouped by kind of work. Nothing is invented: a step we
	 * can't place is shown by its tool.
	 */
	import { onDestroy } from 'svelte';
	import { slide } from 'svelte/transition';
	import { t } from '$lib/i18n';
	import type { AgentRunStatus } from '$lib/apis/chat';
	import {
		buildRuns,
		currentRun,
		latestTodos,
		waitingForPermission,
		type ActivityRun
	} from '$lib/utils/agentActivity';

	interface Props {
		/** The assistant message being generated (null when nothing is running). */
		message: {
			output: any[] | null;
			done: boolean;
			created_at: number;
			agent_status?: AgentRunStatus | null;
		} | null;
		title: string;
	}
	let { message, title }: Props = $props();

	const live = $derived(!!message && !message.done);
	const output = $derived(message?.output ?? []);

	const outputs = $derived.by(() => {
		const map = new Map<string, any>();
		for (const item of output) {
			if (item?.type === 'function_call_output' && item.call_id) map.set(item.call_id, item);
		}
		return map;
	});
	// Every step of the reply, in order (text messages only separate blocks).
	const runs = $derived(
		buildRuns(
			output.filter((i: any) => i?.type === 'function_call' || i?.type === 'reasoning'),
			outputs,
			!live
		)
	);
	const todos = $derived(latestTodos(output));
	const waiting = $derived(
		waitingForPermission(output) || runs.some((r) => r.status === 'approval')
	);
	const current = $derived(currentRun(runs));
	const subagents = $derived(runs.filter((r) => r.kind === 'subagent' && r.status === 'active'));
	const operations = $derived(runs.reduce((n, r) => n + r.calls.length, 0));
	const failedSteps = $derived(runs.filter((r) => r.status === 'failed').length);
	// The latest few steps of real work (plan updates and lone thinking aside).
	const recent = $derived(runs.filter((r) => r.kind !== 'plan').slice(-5));

	// Connection state reported by the adapter (OpenCode 1.x): it outranks
	// "working" because it explains why nothing new is arriving.
	const agentStatus = $derived(message?.agent_status ?? null);
	const connection = $derived(
		agentStatus && ['reconnecting', 'unreachable', 'retrying'].includes(agentStatus.state)
			? agentStatus.state
			: null
	);
	const status = $derived(
		connection ?? (waiting ? 'approval' : current?.kind === 'thinking' ? 'thinking' : 'working')
	);

	// Elapsed time, ticking while the reply runs.
	let now = $state(Date.now());
	const timer = setInterval(() => (now = Date.now()), 1000);
	onDestroy(() => clearInterval(timer));
	function duration(fromMs: number): string {
		const s = Math.max(0, Math.round((now - fromMs) / 1000));
		if (s < 60) return `${s}s`;
		if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
		return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
	}
	const toMs = (t: number) => (t > 1e12 ? t : t * 1000);
	const elapsed = $derived(message?.created_at ? duration(toMs(message.created_at)) : '');
	// How long the current step has been running (OpenCode reports its start).
	const stepElapsed = $derived(
		current && current.status === 'active' && current.startedAt
			? duration(toMs(current.startedAt))
			: ''
	);
	const lastActivityMs = $derived(
		agentStatus?.last_activity ? toMs(agentStatus.last_activity) : null
	);
	const lastActivityText = $derived(
		lastActivityMs
			? new Date(lastActivityMs).toLocaleTimeString([], {
					hour: '2-digit',
					minute: '2-digit',
					second: '2-digit'
				})
			: ''
	);

	// Collapsed or not, remembered per browser.
	const KEY = 'cptr:liveProgress:collapsed';
	let collapsed = $state(
		(() => {
			try {
				return localStorage.getItem(KEY) === '1';
			} catch {
				return false;
			}
		})()
	);
	function toggle() {
		collapsed = !collapsed;
		try {
			localStorage.setItem(KEY, collapsed ? '1' : '0');
		} catch {
			// storage unavailable
		}
	}

	const STATUS_CLASS: Record<string, string> = {
		working: 'text-blue-600 dark:text-blue-400',
		thinking: 'text-gray-500 dark:text-gray-400',
		approval: 'text-amber-600 dark:text-amber-400',
		retrying: 'text-amber-600 dark:text-amber-400',
		reconnecting: 'text-amber-600 dark:text-amber-400',
		unreachable: 'text-red-600 dark:text-red-400'
	};
	function mark(run: ActivityRun): string {
		if (run.status === 'active') return 'live';
		if (run.status === 'approval') return 'wait';
		if (run.status === 'failed') return 'fail';
		return 'done';
	}
</script>

{#if live}
	<div
		class="live-progress mx-auto mb-2 w-full overflow-hidden rounded-xl border text-xs"
		transition:slide={{ duration: 160 }}
		role="status"
		aria-live="polite"
		aria-label={$t('chat.progress.aria', { title })}
	>
		<button
			type="button"
			class="flex w-full items-center gap-2 px-3 py-2 text-left"
			aria-expanded={!collapsed}
			onclick={toggle}
		>
			<span class="dot {status}" aria-hidden="true"></span>
			<span class="min-w-0 flex-1 truncate font-medium text-gray-800 dark:text-gray-100"
				>{title || $t('chat.progress.untitled')}</span
			>
			<span class="shrink-0 font-medium {STATUS_CLASS[status]}"
				>{$t(`chat.progress.status.${status}`)}</span
			>
			<span class="shrink-0 tabular-nums text-gray-400">{elapsed}</span>
			<svg
				xmlns="http://www.w3.org/2000/svg"
				fill="none"
				viewBox="0 0 24 24"
				stroke-width="3"
				stroke="currentColor"
				class="size-2.5 shrink-0 text-gray-400 transition-transform duration-150 {collapsed
					? ''
					: 'rotate-180'}"
				aria-hidden="true"
				><path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" /></svg
			>
		</button>

		{#if collapsed}
			{#if current}
				<p class="truncate px-3 pb-2 -mt-1 text-gray-500 dark:text-gray-400">
					{current.label}{current.latest ? ` · ${current.latest}` : ''}{stepElapsed
						? ` · ${stepElapsed}`
						: ''}
				</p>
			{/if}
		{:else}
			<div class="space-y-2 px-3 pb-2.5" transition:slide={{ duration: 140 }}>
				{#if todos}
					<!-- The agent's own plan. -->
					<ol class="space-y-1">
						{#each todos as todo, i (i)}
							<li class="flex items-start gap-2">
								<span class="todo-mark {todo.status}" aria-hidden="true"></span>
								<span
									class={todo.status === 'completed'
										? 'text-gray-500 dark:text-gray-400'
										: todo.status === 'in_progress'
											? 'font-medium text-gray-800 dark:text-gray-100'
											: todo.status === 'cancelled'
												? 'text-gray-400 line-through'
												: 'text-gray-600 dark:text-gray-300'}>{todo.content}</span
								>
							</li>
						{/each}
					</ol>
				{:else if recent.length}
					<!-- No plan from the agent: its latest real steps. -->
					<ol class="space-y-1">
						{#each recent as run, i (i)}
							<li class="flex min-w-0 items-center gap-2">
								<span class="step-mark {mark(run)}" aria-hidden="true"></span>
								<span
									class="shrink-0 {run.status === 'active'
										? 'font-medium text-gray-800 dark:text-gray-100'
										: run.status === 'failed'
											? 'text-red-600 dark:text-red-400'
											: 'text-gray-600 dark:text-gray-300'}">{run.label}</span
								>
								{#if run.calls.length > 1}
									<span class="shrink-0 text-gray-400"
										>{$t('chat.activity.operations', { count: run.calls.length })}</span
									>
								{/if}
							</li>
						{/each}
					</ol>
				{:else}
					<p class="text-gray-500 dark:text-gray-400">{$t('chat.progress.starting')}</p>
				{/if}

				{#if current && current.kind !== 'thinking'}
					<p class="flex min-w-0 items-center gap-1.5 text-gray-500 dark:text-gray-400">
						<span class="shrink-0">{$t('chat.progress.now')}</span>
						<span class="shrink-0"
							>{current.status === 'approval' ? $t('chat.activity.approval') : current.label}</span
						>
						{#if current.latest && current.kind !== 'subagent'}
							<code
								class="min-w-0 truncate rounded bg-black/[0.04] px-1 py-0.5 text-[0.6875rem] dark:bg-white/[0.06]"
								>{current.latest}</code
							>
						{/if}
						{#if stepElapsed}
							<span class="shrink-0 tabular-nums text-gray-400">{stepElapsed}</span>
						{/if}
					</p>
				{/if}
				{#if agentStatus?.state === 'reconnecting'}
					<p class="text-amber-700 dark:text-amber-300">
						{$t('chat.progress.reconnecting', { attempt: agentStatus.attempt ?? 1 })}
					</p>
				{:else if agentStatus?.state === 'unreachable'}
					<p class="text-red-700 dark:text-red-300">{$t('chat.progress.unreachable')}</p>
				{:else if agentStatus?.state === 'retrying'}
					<p class="text-amber-700 dark:text-amber-300">
						{$t('chat.progress.retrying', {
							detail: agentStatus.detail ? `: ${agentStatus.detail}` : ''
						})}
					</p>
				{:else if agentStatus?.state === 'waiting' && lastActivityMs && !waiting}
					<p class="text-gray-500 dark:text-gray-400">
						{$t('chat.progress.quiet', { duration: duration(lastActivityMs) })}
					</p>
				{/if}
				{#if waiting}
					<p class="text-amber-700 dark:text-amber-300">{$t('chat.progress.approvalHint')}</p>
				{/if}
				{#if subagents.length && !(subagents.length === 1 && current === subagents[0])}
					<p class="text-gray-500 dark:text-gray-400">
						{$t('chat.progress.subagents', { count: subagents.length })}:
						{subagents.map((r) => r.label).join(' · ')}
					</p>
				{/if}
				<p class="text-[0.6875rem] text-gray-400">
					{$t('chat.activity.operations', { count: operations })}{failedSteps
						? ` · ${$t('chat.progress.failedSteps', { count: failedSteps })}`
						: ''}{lastActivityText
						? ` · ${$t('chat.progress.lastActivity', { time: lastActivityText })}`
						: ''}
				</p>
			</div>
		{/if}
	</div>
{/if}

<style>
	.live-progress {
		border-color: color-mix(in oklab, var(--app-fg) 10%, transparent);
		background: color-mix(in oklab, var(--app-bg) 94%, var(--app-fg));
	}
	.dot {
		width: 0.5rem;
		height: 0.5rem;
		flex-shrink: 0;
		border-radius: 9999px;
	}
	.dot.working {
		background: rgb(59 130 246);
		animation: pulse 1.4s ease-out infinite;
	}
	.dot.thinking {
		background: rgb(156 163 175);
		animation: pulse-gray 1.6s ease-out infinite;
	}
	.dot.approval,
	.dot.retrying {
		background: rgb(245 158 11);
	}
	.dot.reconnecting {
		background: rgb(245 158 11);
		animation: pulse-amber 1.2s ease-out infinite;
	}
	.dot.unreachable {
		background: rgb(239 68 68);
	}
	@keyframes pulse-amber {
		0% {
			box-shadow: 0 0 0 0 rgb(245 158 11 / 0.5);
		}
		100% {
			box-shadow: 0 0 0 6px rgb(245 158 11 / 0);
		}
	}
	@keyframes pulse {
		0% {
			box-shadow: 0 0 0 0 rgb(59 130 246 / 0.5);
		}
		100% {
			box-shadow: 0 0 0 6px rgb(59 130 246 / 0);
		}
	}
	@keyframes pulse-gray {
		0% {
			box-shadow: 0 0 0 0 rgb(156 163 175 / 0.5);
		}
		100% {
			box-shadow: 0 0 0 6px rgb(156 163 175 / 0);
		}
	}
	.todo-mark,
	.step-mark {
		position: relative;
		margin-top: 0.2rem;
		width: 0.75rem;
		height: 0.75rem;
		flex-shrink: 0;
		border-radius: 9999px;
		border: 1.5px solid rgb(156 163 175);
	}
	.step-mark {
		margin-top: 0;
	}
	.todo-mark.completed,
	.step-mark.done {
		border-color: rgb(16 185 129);
		background: rgb(16 185 129);
	}
	.todo-mark.completed::after,
	.step-mark.done::after {
		content: '';
		position: absolute;
		left: 0.21rem;
		top: 0.08rem;
		width: 0.2rem;
		height: 0.38rem;
		border: solid white;
		border-width: 0 1.5px 1.5px 0;
		transform: rotate(45deg);
	}
	.todo-mark.in_progress,
	.step-mark.live {
		border-color: rgb(59 130 246);
	}
	.todo-mark.in_progress::after,
	.step-mark.live::after {
		content: '';
		position: absolute;
		inset: 0.15rem;
		border-radius: 9999px;
		background: rgb(59 130 246);
	}
	.step-mark.fail {
		border-color: rgb(239 68 68);
		background: rgb(239 68 68);
	}
	.step-mark.wait {
		border-color: rgb(245 158 11);
	}
	.todo-mark.cancelled {
		border-style: dashed;
	}
	@media (prefers-reduced-motion: reduce) {
		.dot.working,
		.dot.thinking,
		.dot.reconnecting {
			animation: none;
		}
	}
</style>
