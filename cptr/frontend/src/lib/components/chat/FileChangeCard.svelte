<script lang="ts">
	/**
	 * A visible diff for one agent file edit (edit_file, multi_edit_file,
	 * write_file, create_file), instead of a collapsed tool row.
	 */
	import Icon from '../Icon.svelte';
	import { tooltip } from '$lib/tooltip';
	import { fileIconName } from '$lib/utils/fileIcon';
	import { diffStats, lineDiff, type DiffLine } from '$lib/utils/lineDiff';
	import { editedText, followEdits, showEdit } from '$lib/stores/followEdits.svelte';

	interface Props {
		item: any;
		pairedOutput?: any;
	}

	let { item, pairedOutput }: Props = $props();

	const COLLAPSED_LINES = 24;

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
	const path = $derived(String(args.path || ''));
	const fileName = $derived(path.split('/').pop() || path || 'file');
	const dir = $derived(path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '');

	const edits = $derived.by((): { before: string; after: string }[] => {
		if (item.name === 'edit_file') {
			return [{ before: String(args.target ?? ''), after: String(args.replacement ?? '') }];
		}
		if (item.name === 'multi_edit_file') {
			let list = args.edits;
			if (typeof list === 'string') {
				try {
					list = JSON.parse(list);
				} catch {
					list = [];
				}
			}
			return (Array.isArray(list) ? list : []).map((e: any) => ({
				before: String(e?.target ?? ''),
				after: String(e?.replacement ?? '')
			}));
		}
		return [{ before: '', after: String(args.content ?? '') }];
	});
	const hunks = $derived(edits.map((e) => lineDiff(e.before, e.after)));
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
	const isToolResultError = (text: string) => /^\s*(error:|exception:|traceback)/i.test(text);
	const failed = $derived(
		item.status === 'failed' || pairedOutput?.status === 'failed' || isToolResultError(output)
	);
	const running = $derived(!pairedOutput && item.status !== 'failed');
	const verb = $derived.by(() => {
		if (running)
			return item.name === 'edit_file' || item.name === 'multi_edit_file' ? 'Editing' : 'Writing';
		if (failed) return 'Edit failed';
		if (item.name === 'create_file') return 'Created';
		if (item.name === 'write_file') return 'Wrote';
		return 'Edited';
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
		if (path) showEdit(path, editedText(item.name, args));
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
			<button
				type="button"
				class="flex items-center gap-1 rounded-full px-1.5 py-0.5 transition-colors {followEdits.enabled
					? 'bg-blue-500/10 text-blue-600 dark:text-blue-400'
					: 'app-muted hover:text-gray-700 dark:hover:text-gray-300'}"
				onclick={() => (followEdits.enabled = !followEdits.enabled)}
				aria-pressed={followEdits.enabled}
				use:tooltip={followEdits.enabled
					? 'Following edits: files open in the editor as the agent changes them'
					: 'Follow edits: open each file in the editor as the agent changes it'}
			>
				<Icon name="eye" size={12} />
				<span>Follow</span>
			</button>
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
								⋯ {line.count} unchanged {line.count === 1 ? 'line' : 'lines'}
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
