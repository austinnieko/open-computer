import { test } from 'node:test';
import assert from 'node:assert/strict';
import { retryRead } from '../src/lib/utils/retryRead.ts';

const noSleep = async () => {};
const res = (status: number) => new Response(null, { status });

test('a dropped connection is retried until it answers', async () => {
	let calls = 0;
	const out = await retryRead(
		async () => {
			calls += 1;
			if (calls < 3) throw new TypeError('Load failed');
			return res(200);
		},
		{ sleep: noSleep }
	);
	assert.equal(out.status, 200);
	assert.equal(calls, 3);
});

test('gives up after the last retry and throws the network error', async () => {
	let calls = 0;
	await assert.rejects(
		retryRead(
			async () => {
				calls += 1;
				throw new TypeError('Load failed');
			},
			{ sleep: noSleep }
		),
		TypeError
	);
	assert.equal(calls, 3);
});

test('a proxy 502 is retried, a real 500 or 404 is not', async () => {
	const seq = [502, 200];
	let calls = 0;
	assert.equal((await retryRead(async () => res(seq[calls++]), { sleep: noSleep })).status, 200);
	for (const status of [500, 404]) {
		let n = 0;
		const out = await retryRead(
			async () => {
				n += 1;
				return res(status);
			},
			{ sleep: noSleep }
		);
		assert.equal(out.status, status);
		assert.equal(n, 1);
	}
});

test('a timeout or cancel is not retried', async () => {
	let calls = 0;
	await assert.rejects(
		retryRead(
			async () => {
				calls += 1;
				throw new DOMException('timed out', 'TimeoutError');
			},
			{ sleep: noSleep }
		)
	);
	assert.equal(calls, 1);
});
