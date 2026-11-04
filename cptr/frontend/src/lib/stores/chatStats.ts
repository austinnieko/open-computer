import { writable } from 'svelte/store';

/** Whether the stats panel beside chats is shown (one setting for all chats). */
const KEY = 'cptr.chatStats';

function read(): boolean {
	try {
		return localStorage.getItem(KEY) !== 'off';
	} catch {
		return true;
	}
}

export const chatStatsOn = writable<boolean>(typeof localStorage === 'undefined' ? true : read());

chatStatsOn.subscribe((on) => {
	try {
		localStorage.setItem(KEY, on ? 'on' : 'off');
	} catch {
		/* private mode */
	}
});
