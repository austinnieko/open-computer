<script lang="ts">
	/**
	 * Live view of one delegate_task call: the brief the main agent sent, what
	 * the sub-agent is doing right now, the result it handed back, and its full
	 * transcript on demand.
	 */
	import { untrack } from 'svelte';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import Icon from '../Icon.svelte';
	import MarkdownRenderer from '$lib/components/markdown/MarkdownRenderer.svelte';
	import AssistantMessage from './AssistantMessage.svelte';
	import { getChat, type ChatMessageRow, type SubagentStatus } from '$lib/apis/chat';
	import { socketStore } from '$lib/stores/socket.svelte';
	import {
		describeActivity,
		findSubagent,
		formatElapsed,
		toMs
	} from '$lib/stores/subagents.svelte';

	interface Props {
		item: any;
		pairedOutput?: any;
		chatId: string | null;
	}

	let { item, pairedOutput, chatId }: Props = $props();

	const args = $derived.by(() => {
		let a = item.arguments;
		if (typeof a === 'string') {
			try {
				a = JSON.parse(a);
			} catch {
				a = {};
			}
		}
		return a || {};
	});
	const outputText = $derived(typeof pairedOutput?.output === 'string' ? pairedOutput.output : '');
	const info = $derived(findSubagent(chatId, item, outputText));

	const status = $derived.by((): SubagentStatus => {
		if (info) return info.status;
		if (item.status === 'failed' || pairedOutput?.status === 'failed') return 'failed';
		if (pairedOutput) return 'done';
		return 'running';
	});
	const background = $derived(info?.mode === 'background' || args.background === true);
	const task = $derived((info?.task || args.task || '').trim());
	const context = $derived((info?.context || args.context || '').trim());
	// Background calls return a JSON handle; the real result lives in the sub-agent chat.
	const result = $derived.by(() => {
		if (info?.result) return info.result;
		if (!outputText || outputText.trimStart().startsWith('{')) return '';
		return outputText;
	});
	const running = $derived(status === 'running' || status === 'queued');

	let now = $state(Date.now());
	$effect(() => {
		if (!running) return;
		const id = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(id);
	});
	const elapsed = $derived.by(() => {
		if (!info) return '';
		const start = toMs(info.started_at);
		const end = running ? now : toMs(info.updated_at);
		return start ? formatElapsed(end - start) : '';
	});

	let showContext = $state(false);
	let showFullResult = $state(false);
	let showTranscript = $state(false);
	let transcript = $state<ChatMessageRow[]>([]);
	let transcriptLoading = $state(false);

	async function loadTranscript() {
		if (!info) return;
		transcriptLoading = transcript.length === 0;
		try {
			const detail = await getChat(info.chat_id);
			transcript = detail.messages;
		} catch {
			/* keep what we have */
		} finally {
			transcriptLoading = false;
		}
	}

	// While the transcript is open, follow the sub-agent's own events live.
	$effect(() => {
		if (!showTranscript || !info) return;
		const childId = info.chat_id;
		untrack(() => void loadTranscript());
		let timer: ReturnType<typeof setTimeout> | null = null;
		const off = socketStore.on('events:chat', (data: any) => {
			if (data?.chat_id !== childId) return;
			if (timer) return;
			timer = setTimeout(
				() => {
					timer = null;
					untrack(() => void loadTranscript());
				},
				data.done ? 50 : 600
			);
		});
		return () => {
			off?.();
			if (timer) clearTimeout(timer);
		};
	});

	const statusLabel: Record<SubagentStatus, string> = {
		queued: 'Queued',
		running: 'Working',
		done: 'Done',
		failed: 'Failed',
		stopped: 'Stopped'
	};
	const statusClass: Record<SubagentStatus, string> = {
		queued: 'text-gray-500 bg-gray-500/10',
		running: 'text-blue-600 dark:text-blue-400 bg-blue-500/10',
		done: 'text-emerald-600 dark:text-emerald-400 bg-emerald-500/10',
		failed: 'text-red-600 dark:text-red-400 bg-red-500/10',
		stopped: 'text-amber-600 dark:text-amber-400 bg-amber-500/10'
	};
</script>

<div
	id={item.call_id ? `subagent-${item.call_id}` : undefined}
	class="app-subtle-surface my-1.5 w-full min-w-0 overflow-hidden rounded-2xl border text-sm"
>
	<!-- Header -->
	<div class="flex items-center gap-2 px-3 pt-2.5 pb-1.5">
		<span class="app-muted flex size-5 shrink-0 items-center justify-center">
			<Icon name="user" size={15} />
		</span>
		<span class="font-medium text-gray-800 dark:text-gray-200">
			{background ? 'Background sub-agent' : 'Sub-agent'}
		</span>
		<span
			class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[0.6875rem] font-medium {statusClass[
				status
			]}"
		>
			{#if status === 'running'}
				<span class="size-1.5 animate-pulse rounded-full bg-current"></span>
			{/if}
			{statusLabel[status]}
		</span>
		<span class="app-muted ml-auto flex shrink-0 items-center gap-2 text-xs tabular-nums">
			{#if info?.tool_calls}
				<span>{info.tool_calls} {info.tool_calls === 1 ? 'step' : 'steps'}</span>
			{/if}
			{#if elapsed}
				<span class="flex items-center gap-1"><Icon name="clock" size={12} />{elapsed}</span>
			{/if}
		</span>
	</div>

	<!-- Main → sub-agent -->
	<div class="px-3 pb-2">
		<div class="app-muted mb-0.5 text-[0.6875rem] font-medium tracking-wide uppercase">
			Main agent → sub-agent
		</div>
		<div
			class="whitespace-pre-wrap text-gray-700 dark:text-gray-300 {showContext
				? ''
				: 'line-clamp-3'}"
		>
			{task || 'No task given'}
		</div>
		{#if context}
			<button
				type="button"
				class="app-muted mt-1 text-xs hover:text-gray-700 dark:hover:text-gray-300"
				onclick={() => (showContext = !showContext)}
			>
				{showContext ? 'Hide context' : 'Show context it was given'}
			</button>
			{#if showContext}
				<div
					transition:slide={{ duration: 200, easing: quintOut }}
					class="app-muted mt-1 max-h-48 overflow-auto rounded-lg border px-2 py-1.5 text-xs whitespace-pre-wrap"
				>
					{context}
				</div>
			{/if}
		{/if}
	</div>

	<!-- Live activity -->
	{#if running && info}
		<div class="mx-3 mb-2 flex items-center gap-2 rounded-lg bg-blue-500/5 px-2 py-1.5 text-xs">
			<span
				class="size-3 shrink-0 animate-spin rounded-full border-2 border-blue-500 border-t-transparent opacity-70"
			></span>
			<span class="truncate text-gray-700 dark:text-gray-300">{describeActivity(info)}</span>
		</div>
	{/if}

	<!-- Sub-agent → main -->
	{#if result}
		<div class="border-t px-3 pt-2 pb-2">
			<div class="app-muted mb-0.5 text-[0.6875rem] font-medium tracking-wide uppercase">
				Sub-agent → main agent
			</div>
			<div class={showFullResult ? '' : 'max-h-28 overflow-hidden'}>
				<MarkdownRenderer content={result} />
			</div>
			{#if result.length > 400}
				<button
					type="button"
					class="app-muted mt-1 text-xs hover:text-gray-700 dark:hover:text-gray-300"
					onclick={() => (showFullResult = !showFullResult)}
				>
					{showFullResult ? 'Show less' : 'Show full result'}
				</button>
			{/if}
		</div>
	{/if}

	<!-- Transcript toggle -->
	{#if info}
		<button
			type="button"
			class="app-muted flex w-full items-center gap-1.5 border-t px-3 py-1.5 text-xs hover:text-gray-700 dark:hover:text-gray-300"
			aria-expanded={showTranscript}
			onclick={() => (showTranscript = !showTranscript)}
		>
			<Icon name={showTranscript ? 'chevron-up' : 'chevron-down'} size={12} />
			{showTranscript ? 'Hide' : running ? 'Watch live' : 'Show'} what the sub-agent did
		</button>
		{#if showTranscript}
			<div
				transition:slide={{ duration: 250, easing: quintOut }}
				class="max-h-[32rem] overflow-y-auto border-t px-3 py-2"
			>
				{#if transcriptLoading}
					<div class="app-muted py-2 text-xs">Loading…</div>
				{:else}
					{#each transcript.filter((m) => m.role === 'assistant') as msg (msg.id)}
						<AssistantMessage
							content={msg.content}
							meta={msg.meta}
							done={msg.done}
							output={msg.output}
							usage={msg.usage}
							chatId={info.chat_id}
							messageId={msg.id}
							createdAt={msg.created_at}
							onapprove={() => {}}
						/>
					{:else}
						<div class="app-muted py-2 text-xs">Nothing yet.</div>
					{/each}
				{/if}
			</div>
		{/if}
	{/if}
</div>
