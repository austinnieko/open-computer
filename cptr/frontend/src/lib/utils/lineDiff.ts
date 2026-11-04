/**
 * Minimal line diff for showing agent edits: LCS over lines, with unchanged
 * runs collapsed to a few lines of context.
 */

export type DiffLine =
	| { kind: 'same' | 'add' | 'del'; text: string }
	| { kind: 'skip'; count: number };

const MAX_CELLS = 400_000; // LCS table limit; beyond this fall back to remove-all/add-all

function splitLines(text: string): string[] {
	if (!text) return [];
	const lines = text.replace(/\r\n/g, '\n').split('\n');
	if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
	return lines;
}

function rawDiff(a: string[], b: string[]): DiffLine[] {
	// Trim common prefix/suffix first; edits are usually small inside big blocks.
	let start = 0;
	while (start < a.length && start < b.length && a[start] === b[start]) start++;
	let endA = a.length;
	let endB = b.length;
	while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
		endA--;
		endB--;
	}
	const head: DiffLine[] = a.slice(0, start).map((text) => ({ kind: 'same', text }));
	const tail: DiffLine[] = a.slice(endA).map((text) => ({ kind: 'same', text }));
	const midA = a.slice(start, endA);
	const midB = b.slice(start, endB);

	if (midA.length * midB.length > MAX_CELLS) {
		return [
			...head,
			...midA.map((text): DiffLine => ({ kind: 'del', text })),
			...midB.map((text): DiffLine => ({ kind: 'add', text })),
			...tail
		];
	}

	const n = midA.length;
	const m = midB.length;
	const lcs: Uint32Array[] = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
	for (let i = n - 1; i >= 0; i--) {
		for (let j = m - 1; j >= 0; j--) {
			lcs[i][j] =
				midA[i] === midB[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
		}
	}
	const mid: DiffLine[] = [];
	let i = 0;
	let j = 0;
	while (i < n && j < m) {
		if (midA[i] === midB[j]) {
			mid.push({ kind: 'same', text: midA[i] });
			i++;
			j++;
		} else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
			mid.push({ kind: 'del', text: midA[i++] });
		} else {
			mid.push({ kind: 'add', text: midB[j++] });
		}
	}
	while (i < n) mid.push({ kind: 'del', text: midA[i++] });
	while (j < m) mid.push({ kind: 'add', text: midB[j++] });
	return [...head, ...mid, ...tail];
}

/** Diff two texts, keeping `context` unchanged lines around each change. */
export function lineDiff(before: string, after: string, context = 3): DiffLine[] {
	const lines = rawDiff(splitLines(before), splitLines(after));
	const keep = lines.map(() => false);
	lines.forEach((line, idx) => {
		if (line.kind === 'add' || line.kind === 'del') {
			for (
				let k = Math.max(0, idx - context);
				k <= Math.min(lines.length - 1, idx + context);
				k++
			) {
				keep[k] = true;
			}
		}
	});
	const out: DiffLine[] = [];
	let skipped = 0;
	lines.forEach((line, idx) => {
		if (keep[idx]) {
			if (skipped) out.push({ kind: 'skip', count: skipped });
			skipped = 0;
			out.push(line);
		} else {
			skipped++;
		}
	});
	if (skipped && out.length) out.push({ kind: 'skip', count: skipped });
	return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
	let added = 0;
	let removed = 0;
	for (const line of lines) {
		if (line.kind === 'add') added++;
		else if (line.kind === 'del') removed++;
	}
	return { added, removed };
}
