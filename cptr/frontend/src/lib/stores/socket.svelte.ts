/**
 * Shared Socket.IO connection: singleton managed by layout.
 *
 * Any store (chat, future features) registers listeners on this.
 * Layout calls connect() on mount, disconnect() on destroy.
 */

import { io, type Socket } from 'socket.io-client';

let socket: Socket | null = null;
let _connected = $state(false);
type SocketHandler = (...args: any[]) => void;
const registeredListeners = new Map<string, Set<SocketHandler>>();

function bindRegisteredListeners(currentSocket: Socket) {
	for (const [event, handlers] of registeredListeners) {
		for (const handler of handlers) {
			currentSocket.on(event, handler);
		}
	}
}

function connect() {
	if (socket) return;
	socket = io({
		reconnection: true,
		reconnectionDelay: 1000,
		withCredentials: true,
		transports: ['websocket', 'polling'],
		tryAllTransports: true
	});

	socket.on('connect', () => {
		_connected = true;
	});

	socket.on('disconnect', () => {
		_connected = false;
	});

	bindRegisteredListeners(socket);
}

function disconnect() {
	const currentSocket = socket;
	socket = null;
	currentSocket?.disconnect();
	_connected = false;
}

/**
 * Drop the connection and open a fresh one. After a phone suspends the app,
 * the old socket can look connected while it is dead (Socket.IO only notices
 * after its ping timeout, up to ~45s), so no events arrive and the app looks
 * frozen. Reconnecting fires 'connect' again, which every store uses to refetch.
 */
function reconnect() {
	if (!socket) return;
	socket.disconnect();
	socket.connect();
}

/**
 * Whether the connection still works: the server answers a ping in time.
 * A socket a phone suspended can look connected while it is dead.
 */
async function isAlive(timeoutMs = 2000): Promise<boolean> {
	if (!socket?.connected) return false;
	try {
		await socket.timeout(timeoutMs).emitWithAck('cptr:ping');
		return true;
	} catch {
		return false;
	}
}

/** Get the raw socket instance for registering listeners. */
function getSocket(): Socket | null {
	return socket;
}

function on(event: string, handler: SocketHandler): () => void {
	let handlers = registeredListeners.get(event);
	if (!handlers) {
		handlers = new Set();
		registeredListeners.set(event, handlers);
	}
	const wasRegistered = handlers.has(handler);
	handlers.add(handler);
	if (socket && !wasRegistered) {
		socket.on(event, handler);
	}
	return () => off(event, handler);
}

function off(event: string, handler: SocketHandler) {
	const handlers = registeredListeners.get(event);
	handlers?.delete(handler);
	if (handlers?.size === 0) {
		registeredListeners.delete(event);
	}
	socket?.off(event, handler);
}

export const socketStore = {
	connect,
	disconnect,
	reconnect,
	isAlive,
	getSocket,
	on,
	off,
	get connected() {
		return _connected;
	}
};
