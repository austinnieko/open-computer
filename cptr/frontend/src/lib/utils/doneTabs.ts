/**
 * Chat tabs to close because their task is finished.
 *
 * A task marked done (or ended) doesn't need its chat open any more. Its tab is
 * closed once, when it is not the tab on screen in its pane; the chat itself is
 * kept and can be opened again from the sidebar. A chat closed this way is
 * remembered, so a tab you open again for it stays open.
 */

export const DONE_TASK_STATUSES = new Set(['completed', 'ended']);

interface TabLike {
	id: string;
	type: string;
	path?: string;
	permanent?: boolean;
	unsaved?: boolean;
}

interface GroupLike {
	id: string;
	activeTabId: string;
	tabs: TabLike[];
	/** Tab ids, most recently used last. */
	tabHistory?: string[];
}

export function doneTaskChats(tasks: { chat_id?: string | null; status: string }[]): Set<string> {
	const ids = new Set<string>();
	for (const task of tasks) {
		if (task.chat_id && DONE_TASK_STATUSES.has(task.status)) ids.add(task.chat_id);
	}
	return ids;
}

export function tabsToClose(
	groups: GroupLike[],
	doneChats: Set<string>,
	alreadyClosed: Set<string>
): { tabId: string; groupId: string; chatId: string }[] {
	const out: { tabId: string; groupId: string; chatId: string }[] = [];
	for (const group of groups) {
		for (const tab of group.tabs) {
			if (tab.type !== 'chat' || !tab.path || tab.permanent || tab.unsaved) continue;
			if (tab.id === group.activeTabId) continue;
			if (!doneChats.has(tab.path) || alreadyClosed.has(tab.path)) continue;
			out.push({ tabId: tab.id, groupId: group.id, chatId: tab.path });
		}
	}
	return out;
}

/** Chat tabs kept per pane; older ones close. */
export const MAX_CHAT_TABS = 8;

/**
 * Chat tabs beyond the most recently used `keep` in each pane. Every chat
 * opened from the sidebar used to stay open for good. The tab on screen, a chat that is running and a tab with an
 * unsent draft are always kept.
 */
export function overflowChatTabs(
	groups: GroupLike[],
	keep: number,
	opts: { busyChats?: Set<string>; draftTabs?: Set<string> } = {}
): { tabId: string; groupId: string }[] {
	const out: { tabId: string; groupId: string }[] = [];
	for (const group of groups) {
		const chats = group.tabs.filter((t) => t.type === 'chat' && !t.permanent);
		if (chats.length <= keep) continue;
		const history = group.tabHistory ?? [];
		const rank = (id: string) => {
			const i = history.lastIndexOf(id);
			return i < 0 ? -1 : i;
		};
		// Most recently used first; tabs never visited count as oldest, in tab order.
		const ordered = chats
			.map((tab, index) => ({ tab, index }))
			.sort((a, b) => rank(b.tab.id) - rank(a.tab.id) || b.index - a.index)
			.map(({ tab }) => tab);
		for (const tab of ordered.slice(keep)) {
			if (tab.id === group.activeTabId || tab.unsaved) continue;
			if (tab.path && opts.busyChats?.has(tab.path)) continue;
			if (opts.draftTabs?.has(tab.id)) continue;
			out.push({ tabId: tab.id, groupId: group.id });
		}
	}
	return out;
}
