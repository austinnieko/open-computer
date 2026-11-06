/**
 * Unified system events store.
 *
 * Single WebSocket to /api/events/ws that multiplexes:
 * - fs_change events (for FileBrowser auto-refresh)
 * - port_added / port_removed events (for port notifications)
 *
 * Replaces the old fsWatch store.
 */

export interface PortInfo {
	port: number;
	pid: number;
	process: string;
	session_id: string | null;
}

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let currentWatchPath: string | null = null;

// ── FS events ────────────────────────────────────────────────────
let _fsTick = $state(0);
let _fsChangedPaths = $state<string[]>([]);

// ── Port events ──────────────────────────────────────────────────
let _ports = $state<PortInfo[]>([]);
let _newPorts = $state<PortInfo[]>([]);
let _dismissedPorts = new Set<number>();

// Retry delay after a drop: doubles up to a cap, reset once connected, so a
// server that is down isn't hit every 2s forever.
const RETRY_MIN_MS = 2000;
const RETRY_MAX_MS = 30_000;
let retryMs = RETRY_MIN_MS;

function connect(watchPath: string) {
	const samePath = watchPath === currentWatchPath;
	currentWatchPath = watchPath;

	if (ws && ws.readyState <= WebSocket.OPEN) {
		// Already connected, just update watch path (the workspace store emits
		// on every pane click, so skip it when the path hasn't changed).
		if (ws.readyState === WebSocket.OPEN && !samePath) {
			ws.send(JSON.stringify({ type: 'watch_path', path: watchPath }));
		}
		return;
	}

	const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
	const url = `${protocol}//${location.host}/api/events/ws?path=${encodeURIComponent(watchPath)}`;

	ws = new WebSocket(url);
	ws.onopen = () => {
		retryMs = RETRY_MIN_MS;
	};

	ws.onmessage = (event) => {
		try {
			const msg = JSON.parse(event.data);

			if (msg.type === 'fs_change') {
				_fsChangedPaths = msg.paths ?? [];
				_fsTick++;
			} else if (msg.type === 'port_added') {
				const info: PortInfo = {
					port: msg.port,
					pid: msg.pid,
					process: msg.process,
					session_id: msg.session_id ?? null
				};
				// Add to active ports
				_ports = [..._ports.filter((p) => p.port !== info.port), info];
				// Add to new ports (for notification) unless dismissed
				if (!_dismissedPorts.has(info.port)) {
					_newPorts = [..._newPorts.filter((p) => p.port !== info.port), info];
				}
			} else if (msg.type === 'port_removed') {
				_ports = _ports.filter((p) => p.port !== msg.port);
				_newPorts = _newPorts.filter((p) => p.port !== msg.port);
				_dismissedPorts.delete(msg.port);
			}
		} catch {}
	};

	ws.onclose = () => {
		ws = null;
		scheduleRetry();
	};

	ws.onerror = () => {
		ws?.close();
	};
}

function scheduleRetry() {
	if (reconnectTimer) clearTimeout(reconnectTimer);
	reconnectTimer = setTimeout(() => {
		reconnectTimer = null;
		if (!currentWatchPath) return;
		// In the background, wait: reconnect() runs when the app comes back.
		if (document.hidden) return;
		connect(currentWatchPath);
	}, retryMs);
	retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
}

/**
 * Replace the connection. After iOS suspends the app, the socket can still
 * look open but be dead, so file and port updates silently stop.
 */
function reconnect() {
	const path = currentWatchPath;
	if (!path) return;
	if (reconnectTimer) {
		clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}
	if (ws) {
		ws.onclose = null;
		ws.close();
		ws = null;
	}
	retryMs = RETRY_MIN_MS;
	connect(path);
}

function disconnect() {
	if (reconnectTimer) {
		clearTimeout(reconnectTimer);
		reconnectTimer = null;
	}
	if (ws) {
		ws.onclose = null;
		ws.close();
		ws = null;
	}
	currentWatchPath = null;
}

function watchPath(path: string) {
	currentWatchPath = path;
	if (ws && ws.readyState === WebSocket.OPEN) {
		ws.send(JSON.stringify({ type: 'watch_path', path }));
	}
}

function isRelevantFsChange(targetPath: string): boolean {
	return _fsChangedPaths.some((p) => p === targetPath || p.startsWith(targetPath + '/'));
}

function dismissPort(port: number) {
	_dismissedPorts.add(port);
	_newPorts = _newPorts.filter((p) => p.port !== port);
}

export const systemEvents = {
	connect,
	reconnect,
	disconnect,
	watchPath,

	// FS
	get fsTick() {
		return _fsTick;
	},
	get fsChangedPaths() {
		return _fsChangedPaths;
	},
	isRelevantFsChange,

	// Ports
	get ports() {
		return _ports;
	},
	get newPorts() {
		return _newPorts;
	},
	dismissPort
};
