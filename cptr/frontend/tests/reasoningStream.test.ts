// @ts-nocheck -- run by Node (types stripped), not part of the app build
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyReasoningDelta, mergeReasoningDelta } from '../src/lib/utils/reasoningStream.ts';

const item = (text: string, status = 'in_progress') => ({
	type: 'reasoning',
	id: 'r1',
	status,
	content: [{ type: 'reasoning_text', text }]
});

test('appends new text at its offset', () => {
	const out = [item('Let me ')];
	assert.equal(applyReasoningDelta(out, { id: 'r1', offset: 7, text: 'think' }), true);
	assert.equal(out[0].content[0].text, 'Let me think');
});

test('an overlapping piece (after a reload) is not applied twice', () => {
	const out = [item('Let me think')];
	applyReasoningDelta(out, { id: 'r1', offset: 7, text: 'think it' });
	assert.equal(out[0].content[0].text, 'Let me think it');
	assert.equal(applyReasoningDelta(out, { id: 'r1', offset: 7, text: 'think' }), false);
	assert.equal(out[0].content[0].text, 'Let me think it');
});

test('a piece past the end is skipped until the full item arrives', () => {
	const out = [item('Let')];
	assert.equal(applyReasoningDelta(out, { id: 'r1', offset: 10, text: 'x' }), false);
	assert.equal(out[0].content[0].text, 'Let');
});

test('completed or unknown items are left alone', () => {
	const done = [item('final', 'completed')];
	assert.equal(applyReasoningDelta(done, { id: 'r1', offset: 5, text: '!' }), false);
	assert.equal(applyReasoningDelta([], { id: 'r1', offset: 0, text: 'x' }), false);
	assert.equal(applyReasoningDelta(undefined, { id: 'r1', offset: 0, text: 'x' }), false);
});

test('an item with no content yet starts from empty', () => {
	const out: any[] = [{ type: 'reasoning', id: 'r1', status: 'in_progress', content: [] }];
	assert.equal(applyReasoningDelta(out, { id: 'r1', offset: 0, text: 'Hm' }), true);
	assert.equal(out[0].content[0].text, 'Hm');
});

test('pieces queued in one frame merge in order', () => {
	let d = mergeReasoningDelta(undefined, { id: 'r1', offset: 3, text: 'ab' });
	d = mergeReasoningDelta(d, { id: 'r1', offset: 5, text: 'cd' });
	assert.deepEqual(d, { id: 'r1', offset: 3, text: 'abcd' });
	d = mergeReasoningDelta(d, { id: 'r1', offset: 4, text: 'XY' });
	assert.deepEqual(d, { id: 'r1', offset: 3, text: 'aXY' });
	const out = [item('012')];
	applyReasoningDelta(out, d);
	assert.equal(out[0].content[0].text, '012aXY');
});
