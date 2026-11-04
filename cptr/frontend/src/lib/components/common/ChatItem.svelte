<script lang="ts">
	/**
	 * Common chat list item used in sidebar and chat history.
	 * Shows title, relative time, optional spinner for active chats,
	 * and an optional context menu button.
	 */
	import type { ChatInfo } from '$lib/apis/chat';
	import { chatStatuses, isChatUnread } from '$lib/stores/chat';
	import Spinner from './Spinner.svelte';
	import { t } from '$lib/i18n';

	interface Props {
		chat: ChatInfo;
		isSelected?: boolean;
		onclick: () => void;
		/** Optional menu button click handler */
		onmenu?: (e: MouseEvent) => void;
		/** Shown as open (in a pane); with onclose, gets a close button. */
		isOpen?: boolean;
		onclose?: () => void;
	}
	let { chat, isSelected = false, onclick, onmenu, isOpen = false, onclose }: Props = $props();
	let status = $derived($chatStatuses.get(chat.id));
	let active = $derived(status?.active ?? chat.is_active ?? false);
	let unread = $derived(
		!isSelected &&
			(status
				? isChatUnread(status)
				: !active && (chat.last_read_at === null || chat.updated_at > chat.last_read_at))
	);

	function formatTime(ts: number): string {
		const now = Date.now();
		const diffMs = now - ts;
		const diffSec = Math.floor(diffMs / 1000);
		if (diffSec < 60) return 'now';
		const diffMin = Math.floor(diffSec / 60);
		if (diffMin < 60) return `${diffMin}m`;
		const diffHr = Math.floor(diffMin / 60);
		if (diffHr < 24) return `${diffHr}h`;
		const diffDay = Math.floor(diffHr / 24);
		if (diffDay < 7) return `${diffDay}d`;
		const d = new Date(ts);
		return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="group/chat flex items-center gap-1.5 w-full h-7 px-2 rounded-lg cursor-pointer transition-colors duration-75
		{isSelected ? 'bg-gray-200/60 dark:bg-white/8' : 'hover:bg-gray-100/60 dark:hover:bg-white/[0.04]'}"
	role="button"
	tabindex="0"
	{onclick}
	onkeydown={(e) => {
		if (e.key === 'Enter') onclick();
	}}
	title={chat.title}
>
	{#if active}
		<Spinner size={10} borderWidth={1.5} class="opacity-50" />
	{/if}
	{#if unread}
		<span class="size-1.5 shrink-0 rounded-full bg-sky-500" aria-hidden="true"></span>
	{/if}
	<span
		class="flex-1 text-xs truncate min-w-0 {isSelected
			? 'font-medium text-gray-900 dark:text-white'
			: unread || isOpen
				? 'font-medium text-gray-800 dark:text-gray-200'
				: 'text-gray-500 dark:text-gray-500'}">{chat.title}</span
	>
	{#if chat.updated_at}
		<span class="text-[0.625rem] text-gray-300 dark:text-gray-700 shrink-0 tabular-nums"
			>{formatTime(chat.updated_at)}</span
		>
	{/if}
	{#if onclose}
		<button
			class="flex items-center justify-center w-5 h-5 rounded shrink-0 text-gray-400 opacity-0 group-hover/chat:opacity-100 {isSelected
				? 'opacity-100'
				: ''} hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-200 dark:hover:bg-white/10 transition-all duration-75"
			onclick={(e) => {
				e.stopPropagation();
				onclose?.();
			}}
			aria-label={$t('bar.closeTab')}
			title={$t('bar.closeTab')}
		>
			<svg
				width="9"
				height="9"
				viewBox="0 0 16 16"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
			>
				<path d="M3 3l10 10M13 3L3 13" />
			</svg>
		</button>
	{/if}
	{#if onmenu}
		<button
			class="flex items-center justify-center w-5 h-5 rounded shrink-0 text-gray-300 dark:text-gray-700 hover:text-gray-500 dark:hover:text-gray-400 hover:bg-gray-200 dark:hover:bg-white/8 transition-all duration-75"
			onclick={(e) => {
				e.stopPropagation();
				onmenu?.(e);
			}}
			aria-label={$t('a11y.chatOptions')}
		>
			<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor">
				<circle cx="3" cy="8" r="1.5" />
				<circle cx="8" cy="8" r="1.5" />
				<circle cx="13" cy="8" r="1.5" />
			</svg>
		</button>
	{/if}
</div>
