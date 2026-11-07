/**
 * Chats deleted in this session (here, in another tab or on another device).
 * Lists filter them out and their tabs close, so a list fetched before the
 * delete finished can't bring a deleted chat back.
 */
import { writable, get } from 'svelte/store';

export const deletedChats = writable<Set<string>>(new Set());

export function markChatDeleted(chatId: string): void {
	if (!chatId || get(deletedChats).has(chatId)) return;
	deletedChats.update((ids) => new Set(ids).add(chatId));
}

export function isChatDeleted(chatId: string): boolean {
	return get(deletedChats).has(chatId);
}
