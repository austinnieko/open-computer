/**
 * Recognize file edits in tool calls, from Computer's own tools and from coding
 * agents (OpenCode, Claude Code, ...), whose steps arrive as `agent_tool` calls
 * shaped `{ title, input }` with each agent's own field names.
 */

import type { DiffLine } from './lineDiff';

export interface FileEdit {
	kind: 'edit' | 'write' | 'patch';
	path: string;
	/** before/after pairs for edits and writes (write: before is ''). */
	edits: { before: string; after: string }[];
	/** Pre-made diff lines for patches. */
	patchLines?: DiffLine[];
	/** Text to find and flash in the editor afterwards. */
	flashText: string;
	/** True for an agent's own tool (status comes from the call, not an output item). */
	native: boolean;
}

const NATIVE = new Set(['edit_file', 'multi_edit_file', 'write_file', 'create_file']);
const AGENT_WRITE = new Set(['write', 'create', 'write_file', 'create_file']);
const AGENT_EDIT = new Set(['edit', 'str_replace', 'str_replace_editor', 'replace', 'edit_file']);
const AGENT_MULTI = new Set(['multiedit', 'multi_edit', 'multi_edit_file']);
const AGENT_PATCH = new Set(['patch', 'apply_patch', 'applypatch', 'apply_diff']);

function parse(raw: any): any {
	if (typeof raw !== 'string') return raw || {};
	try {
		return JSON.parse(raw);
	} catch {
		return {};
	}
}

const first = (obj: any, keys: string[]): string => {
	for (const k of keys) {
		const v = obj?.[k];
		if (typeof v === 'string') return v;
	}
	return '';
};
const pathOf = (input: any) =>
	first(input, ['path', 'filePath', 'file_path', 'filepath', 'file', 'target_file']);
const beforeOf = (e: any) => first(e, ['oldString', 'old_string', 'old_str', 'target', 'old']);
const afterOf = (e: any) => first(e, ['newString', 'new_string', 'new_str', 'replacement', 'new']);

/** Parse a unified or `*** Begin Patch` style patch into diff lines. */
function parsePatch(text: string): { path: string; lines: DiffLine[]; added: string } {
	let path = '';
	const lines: DiffLine[] = [];
	const addedText: string[] = [];
	for (const raw of text.replace(/\r\n/g, '\n').split('\n')) {
		const header =
			raw.match(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/) || raw.match(/^\+\+\+ (?:b\/)?(.+)$/);
		if (header) {
			if (!path) path = header[1].trim();
			continue;
		}
		if (/^(\*\*\*|---|diff |index |@@)/.test(raw)) {
			if (raw.startsWith('@@') && lines.length) lines.push({ kind: 'skip', count: 0 });
			continue;
		}
		if (raw.startsWith('+')) {
			lines.push({ kind: 'add', text: raw.slice(1) });
			addedText.push(raw.slice(1));
		} else if (raw.startsWith('-')) lines.push({ kind: 'del', text: raw.slice(1) });
		else if (raw.startsWith(' ')) lines.push({ kind: 'same', text: raw.slice(1) });
	}
	return { path, lines, added: addedText.join('\n') };
}

/** Return the file edit a tool call makes, or null if it isn't one. */
export function fileEditFromCall(item: any): FileEdit | null {
	if (item?.type !== 'function_call') return null;
	const args = parse(item.arguments);

	if (NATIVE.has(item.name)) {
		if (item.name === 'create_file' && (args.artifact_type || !args.path)) return null;
		const path = String(args.path || '');
		if (!path) return null;
		if (item.name === 'edit_file') {
			const after = String(args.replacement ?? '');
			return {
				kind: 'edit',
				path,
				edits: [{ before: String(args.target ?? ''), after }],
				flashText: after,
				native: false
			};
		}
		if (item.name === 'multi_edit_file') {
			const list = parse(args.edits);
			const edits = (Array.isArray(list) ? list : []).map((e: any) => ({
				before: String(e?.target ?? ''),
				after: String(e?.replacement ?? '')
			}));
			return { kind: 'edit', path, edits, flashText: edits.at(-1)?.after ?? '', native: false };
		}
		const content = String(args.content ?? '');
		return {
			kind: 'write',
			path,
			edits: [{ before: '', after: content }],
			flashText: content.slice(0, 400),
			native: false
		};
	}

	if (item.name !== 'agent_tool' && !item.native_agent) return null;
	const title = String(args.title || '')
		.trim()
		.toLowerCase();
	const input = parse(args.input ?? args.state?.input);
	if (!title || !input || typeof input !== 'object') return null;

	if (AGENT_WRITE.has(title)) {
		const path = pathOf(input);
		const content = first(input, ['content', 'contents', 'text', 'file_text']);
		if (!path) return null;
		return {
			kind: 'write',
			path,
			edits: [{ before: '', after: content }],
			flashText: content.slice(0, 400),
			native: true
		};
	}
	if (AGENT_EDIT.has(title)) {
		const path = pathOf(input);
		if (!path) return null;
		const after = afterOf(input);
		return {
			kind: 'edit',
			path,
			edits: [{ before: beforeOf(input), after }],
			flashText: after,
			native: true
		};
	}
	if (AGENT_MULTI.has(title)) {
		const path = pathOf(input);
		const list = parse(input.edits);
		if (!path || !Array.isArray(list)) return null;
		const edits = list.map((e: any) => ({ before: beforeOf(e), after: afterOf(e) }));
		return { kind: 'edit', path, edits, flashText: edits.at(-1)?.after ?? '', native: true };
	}
	if (AGENT_PATCH.has(title)) {
		const text = first(input, ['patch', 'patchText', 'patch_text', 'diff', 'input', 'content']);
		if (!text) return null;
		const parsed = parsePatch(text);
		const path = pathOf(input) || parsed.path;
		if (!path) return null;
		return {
			kind: 'patch',
			path,
			edits: [],
			patchLines: parsed.lines,
			flashText: parsed.added.slice(0, 400),
			native: true
		};
	}
	return null;
}

/** Whether a call has finished, judged the right way for its source. */
export function editStatus(item: any, output: any, edit: FileEdit): 'running' | 'done' | 'failed' {
	const status = String(item?.status || '');
	if (status === 'failed' || output?.status === 'failed') return 'failed';
	if (edit.native) return status === 'completed' ? 'done' : 'running';
	if (output) {
		const text = typeof output.output === 'string' ? output.output : '';
		return /^\s*(error:|exception:|traceback)/i.test(text) ? 'failed' : 'done';
	}
	return 'running';
}
