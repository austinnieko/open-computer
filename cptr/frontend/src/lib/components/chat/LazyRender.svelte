<script lang="ts">
	import type { Snippet } from 'svelte';

	// Mounts its content only once it scrolls near the viewport, so a reply with
	// hundreds of tool steps doesn't build every step up front. Once mounted it
	// stays mounted, so expanded/collapsed state survives scrolling. Browser
	// scroll anchoring keeps the view steady as content above the viewport
	// mounts and grows.
	interface Props {
		children: Snippet;
		/** Render immediately (e.g. the newest steps, or short lists). */
		eager?: boolean;
		/** Placeholder height before first mount, in px. */
		estimate?: number;
	}

	let { children, eager = false, estimate = 40 }: Props = $props();

	let el: HTMLDivElement | undefined = $state();
	let seen = $state(false);
	const mounted = $derived(eager || seen);

	function scrollParent(node: HTMLElement): Element | null {
		let p = node.parentElement;
		while (p) {
			const oy = getComputedStyle(p).overflowY;
			if (oy === 'auto' || oy === 'scroll') return p;
			p = p.parentElement;
		}
		return null;
	}

	$effect(() => {
		if (mounted || !el) return;
		if (typeof IntersectionObserver === 'undefined') {
			seen = true;
			return;
		}
		const io = new IntersectionObserver(
			(entries) => {
				if (entries.some((e) => e.isIntersecting)) {
					seen = true;
					io.disconnect();
				}
			},
			{ root: scrollParent(el), rootMargin: '1500px 0px' }
		);
		io.observe(el);
		return () => io.disconnect();
	});
</script>

<div
	bind:this={el}
	style:min-height={mounted ? null : `${estimate}px`}
	data-lazy={mounted ? 'mounted' : 'pending'}
>
	{#if mounted}
		{@render children()}
	{/if}
</div>
