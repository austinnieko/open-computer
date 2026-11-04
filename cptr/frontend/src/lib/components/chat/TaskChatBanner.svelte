<script lang="ts">
	/**
	 * Compact chip in a task chat's header: which coordinated task this chat
	 * belongs to, its branch and live status, and that it runs in the task's
	 * own worktree (not the workspace checkout). Click opens the dashboard on it.
	 */
	import { t } from '$lib/i18n';
	import { tooltip } from '$lib/tooltip';
	import Icon from '../Icon.svelte';
	import { getCoordTask, type CoordTask } from '$lib/apis/coordination';
	import { socketStore } from '$lib/stores/socket.svelte';
	import { coordinationFocus } from '$lib/stores/coordination.svelte';
	import { openCoordinationTab } from '$lib/stores';

	interface Props {
		taskId: string;
	}
	let { taskId }: Props = $props();

	let task = $state<CoordTask | null>(null);

	const STATUS_CLASS: Record<string, string> = {
		queued: 'bg-gray-100 text-gray-600 dark:bg-white/8 dark:text-gray-300',
		running: 'bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300',
		blocked: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300',
		paused: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
		reviewing: 'bg-violet-50 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300',
		integration_ready:
			'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
		completed: 'bg-gray-100 text-gray-500 dark:bg-white/6 dark:text-gray-400',
		failed: 'bg-red-100 text-red-800 dark:bg-red-500/25 dark:text-red-200'
	};

	async function load(id: string) {
		try {
			const result = await getCoordTask(id);
			if (id === taskId) task = result.task;
		} catch {
			task = null; // not ours, deleted, or no access: show nothing
		}
	}

	$effect(() => {
		const id = taskId;
		void load(id);
		return socketStore.on('events:chat', (data: any) => {
			if (data?.type !== 'coord:task') return;
			if (data.task?.id === id) task = data.task;
			else if (data.task === null) void load(id);
		});
	});

	const tip = $derived(
		task
			? [
					$t('coordination.chipTooltip', { title: task.title }),
					task.worktree_path ? `${$t('coordination.worktree')}: ${task.worktree_path}` : '',
					task.blockers.length
						? `${$t('coordination.blockers')}: ${task.blockers.map((b) => b.detail ?? b.kind).join('; ')}`
						: ''
				]
					.filter(Boolean)
					.join('\n')
			: ''
	);

	function openDashboard() {
		if (!task) return;
		coordinationFocus.set(task.id);
		openCoordinationTab();
	}
</script>

{#if task}
	<button
		type="button"
		class="flex min-w-0 shrink items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-[0.6875rem] text-gray-600 transition-colors hover:bg-black/4 dark:text-gray-300 dark:hover:bg-white/6"
		style="border-color: color-mix(in oklab, var(--app-fg) 12%, transparent);"
		use:tooltip={tip}
		aria-label={tip}
		onclick={openDashboard}
	>
		<Icon name="git-branch" size={12} class="shrink-0 text-gray-500" />
		<span class="shrink-0 font-medium">{$t('coordination.chipLabel')}</span>
		{#if task.branch}
			<span class="min-w-0 truncate font-mono text-gray-500 dark:text-gray-400">{task.branch}</span>
		{:else}
			<span class="min-w-0 truncate text-gray-500">{$t('coordination.readOnly')}</span>
		{/if}
		<span
			class="shrink-0 rounded-full px-1.5 text-[0.625rem] font-medium {STATUS_CLASS[task.status]}"
		>
			{$t(`coordination.status.${task.status}`)}
		</span>
	</button>
{/if}
