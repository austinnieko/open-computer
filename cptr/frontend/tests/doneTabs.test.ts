import { test } from 'node:test';
import assert from 'node:assert/strict';
import { doneTaskChats, tabsToClose } from '../src/lib/utils/doneTabs.ts';

const groups = [
	{
		id: 'g1',
		activeTabId: 't-on-screen',
		tabs: [
			{ id: 't-on-screen', type: 'chat', path: 'chat-done-a' },
			{ id: 't-bg-done', type: 'chat', path: 'chat-done-b' },
			{ id: 't-bg-running', type: 'chat', path: 'chat-running' },
			{ id: 't-file', type: 'file', path: 'chat-done-b' },
			{ id: 't-pinned', type: 'chat', path: 'chat-done-c', permanent: true }
		]
	},
	{ id: 'g2', activeTabId: 'x', tabs: [{ id: 't-other-pane', type: 'chat', path: 'chat-done-a' }] }
];

const done = doneTaskChats([
	{ chat_id: 'chat-done-a', status: 'completed' },
	{ chat_id: 'chat-done-b', status: 'ended' },
	{ chat_id: 'chat-done-c', status: 'completed' },
	{ chat_id: 'chat-running', status: 'running' },
	{ chat_id: null, status: 'completed' }
]);

test('only finished tasks count', () => {
	assert.deepEqual([...done].sort(), ['chat-done-a', 'chat-done-b', 'chat-done-c']);
});

test('closes background tabs of finished tasks, never the one on screen', () => {
	const ids = tabsToClose(groups, done, new Set()).map((t) => t.tabId);
	assert.deepEqual(ids, ['t-bg-done', 't-other-pane']);
});

test('a chat closed once is left alone if opened again', () => {
	const ids = tabsToClose(groups, done, new Set(['chat-done-b'])).map((t) => t.tabId);
	assert.deepEqual(ids, ['t-other-pane']);
});

test('keeps the most recently used chat tabs and closes the rest', async () => {
	const { overflowChatTabs } = await import('../src/lib/utils/doneTabs.ts');
	const tabs = Array.from({ length: 6 }, (_, i) => ({
		id: `c${i}`,
		type: 'chat',
		path: `chat${i}`
	}));
	const group = {
		id: 'g',
		activeTabId: 'c0',
		tabs: [{ id: 'files', type: 'files', permanent: true }, ...tabs],
		// c5 used most recently, then c1, c0; c2-c4 never visited
		tabHistory: ['c0', 'c1', 'c5']
	};
	const close = (opts = {}) => overflowChatTabs([group], 2, opts).map((t) => t.tabId);
	// keep c5 and c1; c0 is on screen so it stays too
	assert.deepEqual(close().sort(), ['c2', 'c3', 'c4']);
	assert.deepEqual(close({ busyChats: new Set(['chat3']), draftTabs: new Set(['c4']) }), ['c2']);
	assert.deepEqual(overflowChatTabs([group], 8), []);
});
