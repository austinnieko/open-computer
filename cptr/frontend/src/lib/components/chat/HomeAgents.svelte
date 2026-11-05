<script lang="ts">
	/**
	 * Home: one line about the task agents (how many need you, are running, are
	 * in line…) that opens the Agents dashboard. Hidden when there are none.
	 */
	import { goto } from '$app/navigation';
	import { t } from '$lib/i18n';
	import Icon from '../Icon.svelte';
	import {
		agentCounts,
		agentRows,
		agentsState,
		trackAgents,
		type AgentState
	} from '$lib/stores/agents.svelte';

	/** compact: the welcome page's small style instead of the chat landing's. */
	let { compact = false }: { compact?: boolean } = $props();

	$effect(() => trackAgents());
	const agents = agentsState();
	const counts = $derived(agentCounts(agentRows()));
	const KEYS: AgentState[] = ['needs_you', 'blocked', 'working', 'background', 'in_line', 'review'];
	const DOT: Partial<Record<AgentState, string>> = {
		needs_you: 'bg-amber-500',
		blocked: 'bg-red-500',
		working: 'bg-blue-500',
		background: 'bg-sky-400',
		in_line: 'bg-gray-400',
		review: 'bg-violet-500'
	};
	const parts = $derived(KEYS.filter((k) => counts[k]));
	const labelOf = (s: AgentState) =>
		s === 'review' ? $t('coordination.stage.review') : $t(`coordination.activity.${s}`);
</script>

{#if agents.loaded && parts.length}
	<button
		type="button"
		class="group flex w-full items-center gap-3 rounded-xl border border-gray-100 px-3 py-2 text-left transition-colors duration-75 hover:bg-gray-50 dark:border-white/6 dark:hover:bg-white/4 {compact
			? 'mb-4'
			: 'mt-6'}"
		onclick={() => goto('/agents')}
	>
		<Icon name="cube" size={14} class="shrink-0 text-gray-500" />
		<span class="shrink-0 text-xs font-medium text-gray-700 dark:text-gray-200"
			>{$t('agents.homeSummary')}</span
		>
		<span
			class="flex min-w-0 flex-1 flex-wrap gap-x-3 gap-y-0.5 text-[0.6875rem] text-gray-500 dark:text-gray-400"
		>
			{#each parts as key (key)}
				<span class="inline-flex items-center gap-1">
					<span class="size-1.5 rounded-full {DOT[key]}"></span>
					<span class="tabular-nums">{counts[key]}</span>
					{labelOf(key)}
				</span>
			{/each}
		</span>
		<span
			class="flex shrink-0 items-center gap-0.5 text-[0.6875rem] text-gray-400 group-hover:text-gray-600 dark:group-hover:text-gray-300"
			>{$t('agents.homeOpen')}<Icon name="chevron-right" size={11} /></span
		>
	</button>
{/if}
