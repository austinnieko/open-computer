<script lang="ts">
	/**
	 * A visible diff for one file edit, from Computer's own edit tools or a
	 * coding agent's write/edit/patch step, instead of a collapsed tool row.
	 */
	import Icon from '../Icon.svelte';
	import { tooltip } from '$lib/tooltip';
	import { fileIconName } from '$lib/utils/fileIcon';
	import { diffStats, lineDiff, type DiffLine } from '$lib/utils/lineDiff';
	import { editStatus, type FileEdit } from '$lib/utils/fileEdits';
	import { showEdit } from '$lib/stores/followEdits.svelte';
	import { currentWorkspace } from '$lib/stores';

	interface Props {
		item: any;
		edit: FileEdit;
		pairedOutput?: any;
	}

	let { item, edit, pairedOutput }: Props = $props();

	const COLLAPSED_LINES = 24;

	const path = $derived(edit.path);
	// Show paths inside the workspace relative to it; keep others absolute.
	const shownPath = $derived.by(() => {
		const ws = ($currentWorkspace?.path || '').replace(/\/$/, '');
		return ws && path.startsWith(ws + '/') ? path.slice(ws.length + 1) : path;
	});
	const fileName = $derived(shownPath.split('/').pop() || shownPath || 'file');
	const dir = $derived(
		shownPath.includes('/') ? shownPath.slice(0, shownPath.lastIndexOf('/')) : ''
	);

	const hunks = $derived(
		edit.patchLines ? [edit.patchLines] : edit.edits.map((e) => lineDiff(e.before, e.after))
	);
	const stats = $derived.by(() =>
		hunks.reduce(
			(acc, h) => {
				const s = diffStats(h);
				return { added: acc.added + s.added, removed: acc.removed + s.removed };
			},
			{ added: 0, removed: 0 }
		)
	);
	const totalLines = $derived(hunks.reduce((n, h) => n + h.length, 0));

	const output = $derived(typeof pairedOutput?.output === 'string' ? pairedOutput.output : '');
	const editState = $derived(editStatus(item, pairedOutput, edit));
	const failed = $derived(editState === 'failed');
	const running = $derived(editState === 'running');
	const verb = $derived.by(() => {
		if (running) return edit.kind === 'write' ? 'Writing' : 'Editing';
		if (failed) return 'Edit failed';
		if (item.name === 'create_file') return 'Created';
		return edit.kind === 'write' ? 'Wrote' : 'Edited';
	});

	let expanded = $state(false);
	let open = $state(true);
	const clipped = $derived(!expanded && totalLines > COLLAPSED_LINES);

	function visibleLines(lines: DiffLine[], budget: { left: number }): DiffLine[] {
		if (!clipped) return lines;
		const take = lines.slice(0, Math.max(0, budget.left));
		budget.left -= take.length;
		return take;
	}
	const shown = $derived.by(() => {
		const budget = { left: COLLAPSED_LINES };
		return hunks.map((h) => visibleLines(h, budget));
	});

	function openAtEdit() {
		if (path) showEdit(path, edit.flashText);
	}
</script>

<div
	class="app-subtle-surface my-1.5 w-full min-w-0 overflow-hidden rounded-2xl border text-sm {failed
		? 'border-red-500/30'
		: ''}"
>
	<div class="flex items-center gap-2 px-3 py-2">
		<button
			type="button"
			class="app-muted flex size-5 shrink-0 items-center justify-center"
			onclick={() => (open = !open)}
			aria-label={open ? 'Hide changes' : 'Show changes'}
			aria-expanded={open}
		>
			<Icon name={open ? 'chevron-down' : 'chevron-right'} size={12} />
		</button>
		<span class="app-muted shrink-0">
			{#if running}
				<span
					class="block size-3 animate-spin rounded-full border-2 border-current border-t-transparent opacity-70"
				></span>
			{:else}
				<Icon name={fileIconName(fileName)} size={14} />
			{/if}
		</span>
		<span class="shrink-0 {failed ? 'text-red-600 dark:text-red-400' : 'app-muted'}">{verb}</span>
		<button
			type="button"
			class="min-w-0 truncate text-left font-medium text-gray-800 hover:underline dark:text-gray-200"
			onclick={openAtEdit}
			use:tooltip={'Open at this change'}
		>
			{fileName}{#if dir}<span class="app-muted ml-1.5 font-normal">{dir}</span>{/if}
		</button>
		<span class="ml-auto flex shrink-0 items-center gap-2 text-xs tabular-nums">
			{#if stats.added}<span class="text-emerald-600 dark:text-emerald-400">+{stats.added}</span
				>{/if}
			{#if stats.removed}<span class="text-red-600 dark:text-red-400">−{stats.removed}</span>{/if}
		</span>
	</div>

	{#if open}
		{#if failed && output}
			<div class="border-t px-3 py-1.5 text-xs text-red-600 dark:text-red-400">{output}</div>
		{/if}
		{#if totalLines > 0}
			<div class="overflow-x-auto border-t font-mono text-[0.6875rem] leading-relaxed">
				{#each shown as lines, h (h)}
					{#if h > 0 && lines.length}
						<div class="app-muted border-t border-dashed px-3 py-0.5 text-[10px]">Edit {h + 1}</div>
					{/if}
					{#each lines as line, i (i)}
						{#if line.kind === 'skip'}
							<div class="app-muted bg-gray-500/5 px-3 py-0.5 text-[10px] select-none">
								{line.count
									? `⋯ ${line.count} unchanged ${line.count === 1 ? 'line' : 'lines'}`
									: '⋯'}
							</div>
						{:else}
							<div
								class="flex px-1 whitespace-pre {line.kind === 'add'
									? 'bg-emerald-500/10 text-emerald-800 dark:text-emerald-300'
									: line.kind === 'del'
										? 'bg-red-500/10 text-red-800 dark:text-red-300'
										: 'text-gray-600 dark:text-gray-400'}"
							>
								<span class="w-4 shrink-0 text-center opacity-60 select-none"
									>{line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : ' '}</span
								><span>{line.text || ' '}</span>
							</div>
						{/if}
					{/each}
				{/each}
			</div>
			{#if totalLines > COLLAPSED_LINES}
				<button
					type="button"
					class="app-muted w-full border-t px-3 py-1 text-left text-xs hover:text-gray-700 dark:hover:text-gray-300"
					onclick={() => (expanded = !expanded)}
				>
					{expanded ? 'Show less' : `Show all ${totalLines} lines`}
				</button>
			{/if}
		{:else if !failed}
			<div class="app-muted border-t px-3 py-1.5 text-xs">No text changes.</div>
		{/if}
	{/if}
</div>
