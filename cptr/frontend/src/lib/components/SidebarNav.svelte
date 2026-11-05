<script lang="ts">
	import { goto } from '$app/navigation';
	import { sidebarOpen, showSearch } from '$lib/stores';
	import { chatEnabled } from '$lib/stores/chat';
	import { keybindings, formatChord } from '$lib/stores/keybindings';
	import { t } from '$lib/i18n';
	import Icon from './Icon.svelte';
	import KeyPill from './KeyPill.svelte';
	import { page } from '$app/state';
	import { agentCounts, agentRows, trackAgents } from '$lib/stores/agents.svelte';

	// Live count of agents that need you, shown on the Agents item.
	$effect(() => trackAgents());
	const agentNeeds = $derived.by(() => {
		const c = agentCounts(agentRows());
		return (c.needs_you ?? 0) + (c.blocked ?? 0);
	});
	const agentRunning = $derived.by(() => {
		const c = agentCounts(agentRows());
		return (c.working ?? 0) + (c.background ?? 0);
	});

	function openAgents(e: MouseEvent) {
		e.preventDefault();
		goto('/agents');
		if (typeof window !== 'undefined' && window.innerWidth < 768) sidebarOpen.set(false);
	}

	let searchShortcut = $derived(formatChord($keybindings.quickOpen));

	function openAutomations(e: MouseEvent) {
		e.preventDefault();
		goto('/scheduled');
		if (typeof window !== 'undefined' && window.innerWidth < 768) sidebarOpen.set(false);
	}
</script>

<div class="px-1.5 mt-1 shrink-0">
	<button
		class="group flex items-center gap-1.5 chat-row w-full h-7 px-2 rounded-lg text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-colors duration-100"
		onclick={() => showSearch.set(true)}
	>
		<Icon name="search" size={14} />
		<span class="flex-1 text-left overflow-hidden text-ellipsis whitespace-nowrap"
			>{$t('search.search')}</span
		>
		<KeyPill
			text={searchShortcut}
			class="touch-hide ml-auto shrink-0 opacity-0 group-hover:opacity-100 transition-opacity duration-100"
		/>
	</button>
</div>

<div class="px-1.5 shrink-0">
	<a
		href="/agents"
		class="flex items-center gap-1.5 chat-row w-full h-7 px-2 rounded-lg text-xs transition-colors duration-100 no-underline {page
			.url.pathname === '/agents'
			? 'bg-black/5 text-gray-800 dark:bg-white/8 dark:text-gray-100'
			: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
		onclick={openAgents}
	>
		<Icon name="cube" size={14} />
		<span class="flex-1 text-left overflow-hidden text-ellipsis whitespace-nowrap"
			>{$t('agents.title')}</span
		>
		{#if agentRunning}
			<span class="flex items-center gap-1 text-[0.625rem] tabular-nums text-gray-400">
				<span class="size-1.5 rounded-full bg-blue-500"></span>{agentRunning}
			</span>
		{/if}
		{#if agentNeeds}
			<span
				class="min-w-4 rounded-full bg-amber-500/15 px-1 text-center text-[0.625rem] font-medium tabular-nums text-amber-700 dark:text-amber-300"
				>{agentNeeds}</span
			>
		{/if}
	</a>
</div>

{#if $chatEnabled}
	<div class="px-1.5 shrink-0">
		<a
			href="/scheduled"
			class="flex items-center gap-1.5 chat-row w-full h-7 px-2 rounded-lg text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-colors duration-100 no-underline"
			onclick={openAutomations}
		>
			<Icon name="clock" size={14} />
			<span class="flex-1 text-left overflow-hidden text-ellipsis whitespace-nowrap"
				>{$t('automations.title')}</span
			>
		</a>
	</div>
{/if}
