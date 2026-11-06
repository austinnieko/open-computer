// @ts-nocheck -- run by Node (types stripped), not part of the app build
// Run: npm run test:unit   (node --test, Node 22.6+ strips the types)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	buildRuns,
	classify,
	currentRun,
	latestTodos,
	normalizeCall,
	redact,
	summarizeRuns,
	waitingForPermission
} from '../src/lib/utils/agentActivity.ts';

// Shapes as the adapters produce them (cptr/utils/agents/opencode_v2.py, opencode.py).
let n = 0;
const v2 = (tool: string, input: Record<string, any>, status = 'completed') => ({
	type: 'function_call',
	call_id: `c${++n}`,
	name: 'agent_tool',
	status,
	arguments: { title: tool, input }
});
const v1 = (tool: string, input: Record<string, any>, extra: Record<string, any> = {}) => ({
	type: 'function_call',
	call_id: `c${++n}`,
	name: 'agent_tool',
	status: extra.status ?? 'completed',
	arguments: {
		title: tool,
		state: { status: 'completed', input, title: extra.title ?? '', time: extra.time }
	}
});
const native = (name: string, args: Record<string, any>) => ({
	type: 'function_call',
	call_id: `c${++n}`,
	name,
	status: 'completed',
	arguments: JSON.stringify(args)
});
const thought = (status = 'completed') => ({ type: 'reasoning', id: `r${++n}`, status });
const kind = (item: any) => classify(normalizeCall(item)).kind;

test('classifies shell commands by what they do', () => {
	assert.equal(kind(v2('shell', { command: 'ls -la src' })), 'inspect');
	assert.equal(kind(v2('bash', { command: 'find . -name "*.svelte"' })), 'search');
	assert.equal(kind(v2('bash', { command: 'rg "onMount" src/routes' })), 'search');
	assert.equal(kind(v2('bash', { command: 'cat src/app.ts' })), 'read');
	assert.equal(kind(v2('bash', { command: 'cat tests/test_api.py' })), 'read_tests');
	assert.equal(kind(v2('bash', { command: 'uv run pytest -q' })), 'test');
	assert.equal(kind(v2('bash', { command: 'cd web && npm test' })), 'test');
	assert.equal(kind(v2('bash', { command: 'npm run build' })), 'build');
	assert.equal(kind(v2('bash', { command: 'npm install three' })), 'install');
	assert.equal(kind(v2('bash', { command: 'git diff --stat' })), 'git_review');
	assert.equal(kind(v2('bash', { command: 'git add -A && git commit -m x' })), 'git_commit');
	assert.equal(kind(v2('bash', { command: 'git push -u origin feat' })), 'git_push');
	assert.equal(kind(v2('bash', { command: 'gh pr create --fill' })), 'pr');
	assert.equal(kind(v2('bash', { command: 'echo hi' })), 'shell');
});

test('the most significant part of a chained command wins', () => {
	assert.equal(kind(v2('bash', { command: 'git status && git push' })), 'git_push');
	assert.equal(kind(v2('bash', { command: 'ls && npm test' })), 'test');
});

test('classifies agent tools and Open Computer tools', () => {
	assert.equal(kind(v2('read', { filePath: '/r/src/App.svelte' })), 'read');
	assert.equal(kind(v2('read', { filePath: '/r/src/App.test.ts' })), 'read_tests');
	assert.equal(kind(v2('edit', { filePath: '/r/a.ts' })), 'write');
	assert.equal(kind(v2('grep', { pattern: 'TODO' })), 'search');
	assert.equal(kind(v2('glob', { pattern: '**/*.ts' })), 'search');
	assert.equal(kind(v2('todowrite', { todos: [] })), 'plan');
	assert.equal(
		kind(v2('task', { description: 'Review the diff', subagent_type: 'reviewer' })),
		'subagent'
	);
	assert.equal(kind(v2('linear_update_issue', {})), 'linear');
	assert.equal(kind(native('run_command', { command: 'pytest' })), 'test');
	assert.equal(kind(native('read_file', { path: 'a.py' })), 'read');
	assert.equal(kind(native('edit_file', { path: 'a.py' })), 'write');
	assert.equal(kind(v2('mcp_weather', {})), 'other');
});

test('OpenCode 1.x calls read input from state and keep their timing', () => {
	const call = normalizeCall(
		v1('bash', { command: 'ls src' }, { title: 'List src', time: { start: 1000, end: 2500 } })
	);
	assert.equal(call.tool, 'bash');
	assert.equal(call.input.command, 'ls src');
	assert.equal(call.title, 'List src');
	assert.equal(call.endedAt! - call.startedAt!, 1500);
});

test('five repository inspections become one run, not five "Shell" rows', () => {
	const entries = [
		thought(),
		v2('shell', { command: 'ls' }),
		thought(),
		v2('shell', { command: 'ls src' }),
		v2('shell', { command: 'tree -L 2' }),
		thought(),
		v2('shell', { command: 'pwd' }),
		v2('shell', { command: 'ls src/lib' })
	];
	const runs = buildRuns(entries, new Map(), true);
	assert.equal(runs.length, 1);
	assert.equal(runs[0].label, 'Inspected repository structure');
	assert.equal(runs[0].calls.length, 5);
	assert.equal(runs[0].reasoning.length, 3);
	// Order is preserved, reasoning included.
	assert.deepEqual(
		runs[0].entries.map((e) => e.type),
		[
			'reasoning',
			'function_call',
			'reasoning',
			'function_call',
			'function_call',
			'reasoning',
			'function_call',
			'function_call'
		]
	);
});

test('unrelated work is never merged, even with the same tool', () => {
	const runs = buildRuns(
		[
			v2('shell', { command: 'ls' }),
			v2('shell', { command: 'npm test' }),
			v2('shell', { command: 'ls' })
		],
		new Map(),
		true
	);
	assert.deepEqual(
		runs.map((r) => r.kind),
		['inspect', 'test', 'inspect']
	);
});

test('each sub-agent call is its own run, labelled with its owner', () => {
	const runs = buildRuns(
		[
			v2('task', { description: 'Review the diff', subagent_type: 'reviewer' }),
			v2('task', { description: 'Write tests', subagent_type: 'tester' }, 'in_progress')
		],
		new Map(),
		false
	);
	assert.equal(runs.length, 2);
	assert.equal(runs[0].label, 'Reviewer sub-agent: Review the diff');
	assert.equal(runs[0].status, 'done');
	assert.equal(runs[1].owner, 'tester');
	assert.equal(runs[1].status, 'active');
});

test('live status: active, failed, approval, and trailing thinking', () => {
	const live = buildRuns(
		[v2('shell', { command: 'npm test' }, 'in_progress'), thought('in_progress')],
		new Map(),
		false
	);
	assert.equal(live[0].status, 'active');
	assert.equal(live[0].label, 'Running tests');
	assert.equal(live[1].kind, 'thinking');
	assert.equal(live[1].status, 'active');

	const failed = buildRuns(
		[
			v2('shell', { command: 'npm test' }, 'completed'),
			v2('shell', { command: 'npm test' }, 'failed')
		],
		new Map(),
		true
	);
	assert.equal(failed[0].status, 'failed');
	assert.equal(failed[0].failed, 1);

	const recovered = buildRuns(
		[
			v2('shell', { command: 'npm test' }, 'failed'),
			v2('shell', { command: 'npm test' }, 'completed')
		],
		new Map(),
		true
	);
	assert.equal(recovered[0].status, 'done'); // the last attempt passed
	assert.equal(recovered[0].failed, 1); // but the failure stays visible

	const approval = buildRuns(
		[v2('bash', { command: 'rm -rf build' }, 'pending')],
		new Map(),
		false
	);
	assert.equal(approval[0].status, 'approval');
});

test('a finished turn never shows anything as still running', () => {
	const runs = buildRuns(
		[v2('shell', { command: 'ls' }, 'in_progress'), thought('in_progress')],
		new Map(),
		true
	);
	assert.ok(runs.every((r) => r.status !== 'active'));
});

test('native tools count as done once their output arrives', () => {
	const call = native('run_command', { command: 'pytest' });
	(call as any).status = 'in_progress';
	const outputs = new Map([[call.call_id, { output: 'ok' }]]);
	assert.equal(buildRuns([call], outputs, false)[0].status, 'done');
	const bad = new Map([[call.call_id, { output: 'Error: no tests' }]]);
	assert.equal(buildRuns([call], bad, false)[0].status, 'failed');
});

test('summary of a finished block', () => {
	const runs = buildRuns(
		[
			v2('shell', { command: 'ls' }),
			v2('read', { filePath: 'a.ts' }),
			v2('edit', { filePath: 'a.ts' }),
			v2('shell', { command: 'npm test' })
		],
		new Map(),
		true
	);
	assert.equal(
		summarizeRuns(runs),
		'Inspected repository structure, read files, made code changes +1 more'
	);
});

test('the agent plan comes from its latest todo list', () => {
	const output = [
		v2('todowrite', { todos: [{ content: 'Old', status: 'pending' }] }),
		{ type: 'message', content: [{ text: 'hi' }] },
		v2('todowrite', {
			todos: [
				{ content: 'Inspect repo', status: 'completed' },
				{ content: 'Evaluate 3D options', status: 'in_progress' },
				{ content: 'Write roadmap', status: 'pending' }
			]
		})
	];
	assert.deepEqual(latestTodos(output), [
		{ content: 'Inspect repo', status: 'completed' },
		{ content: 'Evaluate 3D options', status: 'in_progress' },
		{ content: 'Write roadmap', status: 'pending' }
	]);
	assert.equal(latestTodos([v2('shell', { command: 'ls' })]), null);
});

test('permission waits are detected from the adapter notice', () => {
	const msg = (text: string) => ({ type: 'message', content: [{ text }] });
	assert.equal(
		waitingForPermission([
			v2('bash', { command: 'ls' }),
			msg('\n\n⏸ Waiting for permission: `bash`.')
		]),
		true
	);
	assert.equal(
		waitingForPermission([msg('⏸ Waiting for permission: x'), v2('bash', { command: 'ls' })]),
		false
	);
});

test('secrets are masked in summaries', () => {
	assert.equal(
		redact('curl -H "Authorization: Bearer abc123def456" https://api.x'),
		'curl -H "Authorization: Bearer •••" https://api.x'
	);
	assert.match(redact('export GITHUB_TOKEN=ghp_abcdefghijklmnop1234'), /•••/);
	assert.doesNotMatch(redact('export GITHUB_TOKEN=ghp_abcdefghijklmnop1234'), /abcdefghijklmnop/);
	assert.equal(redact('psql postgres://me:hunter2@db/x'), 'psql postgres://me:•••@db/x');
	assert.match(redact('mysql --password=hunter2'), /--password=•••/);
	const detail = classify(
		normalizeCall(v2('bash', { command: 'curl -H "Authorization: Bearer sk-livetoken12345" x' }))
	).detail;
	assert.doesNotMatch(detail, /livetoken/);
});

test('the current step is real work; "Thinking" only when nothing else runs', () => {
	const busy = buildRuns(
		[v2('shell', { command: 'npm test' }, 'in_progress'), thought('in_progress')],
		new Map(),
		false
	);
	assert.equal(currentRun(busy)?.label, 'Running tests');
	const idle = buildRuns(
		[v2('shell', { command: 'ls' }), thought('in_progress')],
		new Map(),
		false
	);
	assert.equal(currentRun(idle)?.kind, 'thinking');
	assert.equal(currentRun(buildRuns([v2('shell', { command: 'ls' })], new Map(), true)), undefined);
});
