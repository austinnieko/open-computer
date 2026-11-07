/**
 * Retry a read when the request never got an answer.
 *
 * A dropped connection between the browser and the server (a network blip,
 * the app coming back from the background, the server restarting) makes
 * fetch() throw a TypeError: "Load failed" in Safari, "Failed to fetch" in
 * Chrome. Proxies in front of the server answer 502/503/504 for the same
 * reasons. Reads are safe to repeat, so try again a couple of times before
 * the screen shows an error. Real answers (404, 500, ...) are not retried.
 */

export const READ_RETRY_DELAYS_MS = [400, 1500];

const RETRY_STATUSES = new Set([502, 503, 504]);

/** fetch() threw without an HTTP answer (not a timeout or a cancel). */
export function isNetworkError(error: unknown): boolean {
	return error instanceof TypeError;
}

export async function retryRead(
	attempt: () => Promise<Response>,
	opts: {
		delays?: number[];
		signal?: AbortSignal | null;
		sleep?: (ms: number) => Promise<void>;
	} = {}
): Promise<Response> {
	const delays = opts.delays ?? READ_RETRY_DELAYS_MS;
	const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
	for (let i = 0; ; i++) {
		const last = i >= delays.length || !!opts.signal?.aborted;
		try {
			const res = await attempt();
			if (last || !RETRY_STATUSES.has(res.status)) return res;
		} catch (error) {
			if (last || !isNetworkError(error)) throw error;
		}
		await sleep(delays[i]);
	}
}
