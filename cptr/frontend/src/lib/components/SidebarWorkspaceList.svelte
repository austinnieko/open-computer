<script lang="ts">
	import { goto } from '$app/navigation';
	import {
		workspaceList,
		removeWorkspace,
		reorderWorkspaces,
		sidebarOpen,
		activeTab,
		currentWorkspace,
		openChatInSplit,
		openCoordinationTab,
		closeTab
	} from '$lib/stores';
	import { chatEnabled, chatStatuses, updateChatStatuses } from '$lib/stores/chat';
	import { socketStore } from '$lib/stores/socket.svelte';
	import {
		deleteChat as apiDeleteChat,
		getChats,
		updateChatTitle,
		type ChatInfo
	} from '$lib/apis/chat';
	import { t } from '$lib/i18n';
	import { tooltip } from '$lib/tooltip';
	import Sortable from 'sortablejs';
	import { onDestroy, onMount, untrack } from 'svelte';
	import ChatItem from './common/ChatItem.svelte';
	import { requestConfirm } from '$lib/stores/confirm';
	import { describeActivity, subagentsOf, trackSubagents } from '$lib/stores/subagents.svelte';
	import {
		coordinationOf,
		lifecycleOf,
		lifecycleTone,
		trackCoordination
	} from '$lib/stores/coordination.svelte';
	import DropdownMenu from './DropdownMenu.svelte';
	import Icon from './Icon.svelte';

	interface Props {
		onaddworkspace: () => void;
	}

	let { onaddworkspace }: Props = $props();
	let wsMenuPath = $state<string | null>(null);
	let wsMenuAnchor = $state<HTMLElement | null>(null);
	let chatMenu = $state<{ chatId: string; wsPath: string; anchor: HTMLElement } | null>(null);
	let wsListEl: HTMLDivElement | undefined = $state();
	let sortable: Sortable | null = null;
	let unbindSocketListener: (() => void) | null = null;
	let workspacesExpanded = $state(true);

	// Expanded/collapsed workspaces are remembered across reloads. The current
	// workspace opens automatically unless you collapsed it yourself.
	const EXPANDED_KEY = 'cptr.sidebar.expanded';
	const COLLAPSED_KEY = 'cptr.sidebar.collapsed';
	function readSet(key: string): Set<string> {
		try {
			return new Set(JSON.parse(localStorage.getItem(key) || '[]'));
		} catch {
			return new Set();
		}
	}
	function writeSet(key: string, value: Set<string>) {
		try {
			localStorage.setItem(key, JSON.stringify([...value]));
		} catch {
			/* private mode */
		}
	}
	let expandedWorkspaces = $state<Set<string>>(readSet(EXPANDED_KEY));
	let collapsedByUser = readSet(COLLAPSED_KEY);
	// Chats whose sub-agent branch is open (or closed) in the tree.
	let agentBranches = $state<Record<string, boolean>>({});
	let wsChatsCache = $state<Map<string, ChatInfo[]>>(new Map());
	let wsChatsHasMore = $state<Map<string, boolean>>(new Map());
	let wsChatsLoading = $state<Set<string>>(new Set());
	let currentPath = $derived($currentWorkspace?.path ?? null);
	let currentChatId = $derived($activeTab?.type === 'chat' ? $activeTab.path : null);
	const WS_CHATS_PAGE_SIZE = 8;

	// Chats open in the current workspace (what used to be tabs in the top bar),
	// listed first under it. Titles come from the chat list when known.
	type OpenChat = { tabId: string; groupId: string; chat: ChatInfo };
	const openChats = $derived.by((): OpenChat[] => {
		const ws = $currentWorkspace;
		if (!ws) return [];
		const known = new Map((wsChatsCache.get(ws.path) ?? []).map((c) => [c.id, c]));
		const seen = new Set<string>();
		const out: OpenChat[] = [];
		for (const g of ws.groups) {
			for (const t of g.tabs) {
				if (t.type !== 'chat' || !t.path || seen.has(t.path)) continue;
				seen.add(t.path);
				const draft = t.path.startsWith('new-') || t.path.startsWith('pending-');
				const chat: ChatInfo = known.get(t.path) ?? {
					id: t.path,
					title: t.label || (draft ? 'New chat' : 'Chat'),
					summary: null,
					folder: '',
					meta: null,
					current_message_id: null,
					created_at: 0,
					updated_at: 0,
					last_read_at: null
				};
				out.push({ tabId: t.id, groupId: g.id, chat });
			}
		}
		return out;
	});
	// A/C badges: task agent chats vs. normal chats, coloured by status. Task
	// statuses come from the open workspace's Agents data.
	$effect(() => {
		const path = currentPath;
		if (!path) return;
		return trackCoordination(path);
	});
	const taskByChat = $derived.by(() => {
		const map = new Map<string, ReturnType<typeof coordinationOf>['tasks'][number]>();
		if (!currentPath) return map;
		for (const task of coordinationOf(currentPath).tasks) {
			if (task.chat_id) map.set(task.chat_id, task);
		}
		return map;
	});

	function badgeFor(chat: ChatInfo): { letter: string; tone: string; label: string } | undefined {
		if (isDraftId(chat.id)) return undefined;
		const taskId = (chat.meta as Record<string, unknown> | null)?.coord_task_id;
		const task = taskByChat.get(chat.id);
		if (task || taskId) {
			if (!task || !currentPath) return { letter: 'A', tone: 'neutral', label: 'Task agent' };
			const state = coordinationOf(currentPath);
			const life = lifecycleOf(task, {
				titles: new Map(state.tasks.map((t) => [t.id, t.title])),
				runningWriters: state.tasks.filter((t) => t.status === 'running' && t.worktree_path).length,
				maxWriters: state.maxWriters
			});
			const name =
				life.stage === 'active' && life.activity
					? $t(`coordination.activity.${life.activity}`)
					: $t(`coordination.stage.${life.stage}`);
			return { letter: 'A', tone: lifecycleTone(life), label: `Task agent · ${name}` };
		}
		const active = $chatStatuses.get(chat.id)?.active ?? chat.is_active ?? false;
		return active
			? { letter: 'C', tone: 'running', label: 'Chat · Working' }
			: { letter: 'C', tone: 'neutral', label: 'Chat' };
	}
	const isDraftId = (id: string) => id.startsWith('new-') || id.startsWith('pending-');
	const openChatIds = $derived(new Set(openChats.map((o) => o.chat.id)));

	function closeOpenChat(o: OpenChat) {
		void closeTab(o.tabId, o.groupId);
	}

	function toggleWorkspaceExpand(path: string) {
		const next = new Set(expandedWorkspaces);
		if (next.has(path)) {
			next.delete(path);
			collapsedByUser.add(path);
		} else {
			next.add(path);
			collapsedByUser.delete(path);
			if (!wsChatsCache.has(path)) fetchWorkspaceChats(path);
		}
		expandedWorkspaces = next;
		writeSet(EXPANDED_KEY, next);
		writeSet(COLLAPSED_KEY, collapsedByUser);
	}

	// Open the current workspace's tree, and load any remembered ones.
	$effect(() => {
		const path = currentPath;
		if (!$chatEnabled) return;
		if (path && !expandedWorkspaces.has(path) && !collapsedByUser.has(path)) {
			expandedWorkspaces = new Set([...expandedWorkspaces, path]);
		}
		const toLoad = [...expandedWorkspaces];
		untrack(() => {
			for (const p of toLoad) {
				if (!wsChatsCache.has(p) && !wsChatsLoading.has(p)) fetchWorkspaceChats(p);
			}
		});
	});

	// Load sub-agents for every chat listed under an expanded workspace, so the
	// Agents branches show whether or not that chat is open.
	const trackedChats = new Map<string, () => void>();
	$effect(() => {
		const visible = new Set<string>();
		for (const path of expandedWorkspaces) {
			for (const chat of wsChatsCache.get(path) ?? []) visible.add(chat.id);
		}
		untrack(() => {
			for (const id of visible) {
				if (!trackedChats.has(id)) trackedChats.set(id, trackSubagents(id));
			}
			for (const [id, release] of trackedChats) {
				if (!visible.has(id)) {
					release();
					trackedChats.delete(id);
				}
			}
		});
	});
	onDestroy(() => {
		for (const release of trackedChats.values()) release();
		trackedChats.clear();
	});

	function agentBranchOpen(chatId: string): boolean {
		if (chatId in agentBranches) return agentBranches[chatId];
		// Open by default while any of its sub-agents is working.
		return subagentsOf(chatId).some((a) => a.status === 'running' || a.status === 'queued');
	}

	function openSubagent(chatId: string, wsPath: string, callId: string | null) {
		openChat(chatId, wsPath);
		if (!callId) return;
		setTimeout(() => {
			document
				.getElementById(`subagent-${callId}`)
				?.scrollIntoView({ behavior: 'smooth', block: 'center' });
		}, 400);
	}

	async function fetchWorkspaceChats(path: string, append = false, limit = WS_CHATS_PAGE_SIZE) {
		if (wsChatsLoading.has(path)) return;
		wsChatsLoading = new Set([...wsChatsLoading, path]);
		try {
			const existing = wsChatsCache.get(path) ?? [];
			const data = await getChats(
				path,
				append ? WS_CHATS_PAGE_SIZE : limit,
				append ? existing.length : 0,
				'updated_at',
				'desc'
			);
			wsChatsCache = new Map([
				...wsChatsCache,
				[
					path,
					append
						? [...existing, ...(data.chats || [])].sort(
								(a, b) =>
									Number(
										!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)
									) -
										Number(
											!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
										) || b.updated_at - a.updated_at
							)
						: data.chats || []
				]
			]);
			updateChatStatuses(data.chats || [], path);
			wsChatsHasMore = new Map([...wsChatsHasMore, [path, data.has_more]]);
		} catch {
			wsChatsCache = new Map([...wsChatsCache, [path, []]]);
			wsChatsHasMore = new Map([...wsChatsHasMore, [path, false]]);
		} finally {
			const next = new Set(wsChatsLoading);
			next.delete(path);
			wsChatsLoading = next;
		}
	}

	function reloadWorkspaceChats(path: string) {
		const loadedCount = wsChatsCache.get(path)?.length ?? WS_CHATS_PAGE_SIZE;
		void fetchWorkspaceChats(path, false, Math.max(loadedCount, WS_CHATS_PAGE_SIZE));
	}

	function closeMobileSidebar() {
		if (typeof window !== 'undefined' && window.innerWidth < 768) sidebarOpen.set(false);
	}

	function openWorkspace(e: MouseEvent, path: string) {
		if (e.metaKey || e.ctrlKey) return;
		e.preventDefault();
		goto(`/?workspace=${encodeURIComponent(path)}`);
		closeMobileSidebar();
	}

	function openChat(chatId: string, wsPath: string) {
		goto(`/?workspace=${encodeURIComponent(wsPath)}&chatId=${encodeURIComponent(chatId)}`);
		closeMobileSidebar();
	}

	function newChat(wsPath: string) {
		goto(`/?workspace=${encodeURIComponent(wsPath)}&chatId`);
		closeMobileSidebar();
	}

	function openWsMenu(e: MouseEvent, path: string) {
		e.stopPropagation();
		e.preventDefault();
		closeChatMenu();
		wsMenuAnchor = e.currentTarget as HTMLElement;
		wsMenuPath = path;
	}

	function closeWsMenu() {
		wsMenuPath = null;
		wsMenuAnchor = null;
	}

	function openChatMenu(e: MouseEvent, chatId: string, wsPath: string) {
		e.stopPropagation();
		closeWsMenu();
		chatMenu = { chatId, wsPath, anchor: e.currentTarget as HTMLElement };
	}

	function closeChatMenu() {
		chatMenu = null;
	}

	/** Open the workspace, then its Agents & tasks dashboard. */
	async function handleOpenAgents(path: string) {
		closeWsMenu();
		if (currentPath !== path) {
			await goto(`/?workspace=${encodeURIComponent(path)}`);
			// The workspace store switches after navigation; wait briefly for it.
			for (let i = 0; i < 40 && currentPath !== path; i++) {
				await new Promise((r) => setTimeout(r, 50));
			}
		}
		if (currentPath === path) openCoordinationTab();
		closeMobileSidebar();
	}

	async function handleRemoveWorkspace(path: string) {
		closeWsMenu();
		await removeWorkspace(path);
		if (currentPath === path) goto('/');
	}

	async function handleDeleteChat() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		closeChatMenu();
		// A task's agent chat takes its task with it, so say so first.
		const listed = (wsChatsCache.get(wsPath) ?? []).find((c) => c.id === chatId);
		const isTaskChat =
			taskByChat.has(chatId) || !!(listed?.meta as Record<string, unknown> | null)?.coord_task_id;
		if (isTaskChat) {
			const ok = await requestConfirm({
				title: $t('sidebar.deleteTaskChatTitle'),
				message: $t('sidebar.deleteTaskChatMessage'),
				confirmLabel: $t('sidebar.deleteTaskChatConfirm')
			});
			if (!ok) return;
		}
		try {
			await apiDeleteChat(chatId);
		} catch (err) {
			// Already gone (e.g. deleted elsewhere): still clear the stale row.
			if ((err as { status?: number })?.status !== 404) throw err;
		}
		// An open chat is listed from its tab, so close the tab too or the row stays.
		for (const o of openChats.filter((o) => o.chat.id === chatId)) closeOpenChat(o);
		const chats = wsChatsCache.get(wsPath) ?? [];
		wsChatsCache = new Map([...wsChatsCache, [wsPath, chats.filter((chat) => chat.id !== chatId)]]);
		if (currentPath === wsPath && currentChatId === chatId) {
			goto(`/?workspace=${encodeURIComponent(wsPath)}`);
		}
	}

	async function handleRenameChat() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		const chat = (wsChatsCache.get(wsPath) ?? []).find((item) => item.id === chatId);
		const title = window.prompt($t('files.rename'), chat?.title)?.trim();
		if (!title || title === chat?.title) return;
		await updateChatTitle(chatId, title);
		const chats = wsChatsCache.get(wsPath) ?? [];
		wsChatsCache = new Map([
			...wsChatsCache,
			[wsPath, chats.map((item) => (item.id === chatId ? { ...item, title } : item))]
		]);
	}

	/** Show a chat in its own pane next to the current one. */
	function openChatBeside() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		chatMenu = null;
		if (wsPath === currentPath) openChatInSplit(chatId);
		else openChat(chatId, wsPath);
	}

	function copyChatPath() {
		if (!chatMenu) return;
		const { chatId, wsPath } = chatMenu;
		const chat = (wsChatsCache.get(wsPath) ?? []).find((item) => item.id === chatId);
		if (!chat) return;
		navigator.clipboard.writeText(
			`${wsPath.replace(/\/$/, '')}/.cptr/chats/${chat.folder ? `${chat.folder}/` : ''}${chat.id}.json`
		);
	}

	function handleChatEvent(data: {
		type?: string;
		chat_id: string;
		done?: boolean;
		title?: string;
		delta?: string;
		workspace?: string;
		active?: boolean;
		updated_at?: number;
		last_read_at?: number;
		workspace_unread_count?: number;
	}) {
		if (
			!data.title &&
			typeof data.active !== 'boolean' &&
			typeof data.updated_at !== 'number' &&
			typeof data.last_read_at !== 'number' &&
			typeof data.workspace_unread_count !== 'number'
		) {
			return;
		}
		const unreadCount = data.workspace_unread_count;
		if (data.workspace && typeof unreadCount === 'number') {
			workspaceList.update((workspaces) =>
				workspaces.map((workspace) =>
					workspace.path === data.workspace
						? { ...workspace, unread_count: unreadCount }
						: workspace
				)
			);
		}

		let known = false;
		const shouldReorder =
			typeof data.updated_at === 'number' ||
			typeof data.last_read_at === 'number' ||
			typeof data.active === 'boolean';

		wsChatsCache = new Map(
			[...wsChatsCache].map(([path, chats]) => {
				const nextChats = chats.map((chat) => {
					if (chat.id !== data.chat_id) return chat;
					known = true;
					return {
						...chat,
						...(data.title ? { title: data.title } : {}),
						...(typeof data.updated_at === 'number' ? { updated_at: data.updated_at } : {}),
						...(typeof data.last_read_at === 'number' ? { last_read_at: data.last_read_at } : {}),
						...(typeof data.active === 'boolean' ? { is_active: data.active } : {})
					};
				});
				return [
					path,
					shouldReorder
						? nextChats.sort(
								(a, b) =>
									Number(
										!b.is_active && (b.last_read_at === null || b.updated_at > b.last_read_at)
									) -
										Number(
											!a.is_active && (a.last_read_at === null || a.updated_at > a.last_read_at)
										) || b.updated_at - a.updated_at
							)
						: nextChats
				] as [string, ChatInfo[]];
			})
		);

		// A chat created in another session is not yet in this sidebar's page.
		// Refresh only that expanded workspace; all known rows update in place.
		if (!known && data.workspace && expandedWorkspaces.has(data.workspace)) {
			void fetchWorkspaceChats(data.workspace);
		} else if (
			known &&
			typeof data.last_read_at === 'number' &&
			data.workspace &&
			expandedWorkspaces.has(data.workspace)
		) {
			reloadWorkspaceChats(data.workspace);
		}
	}

	function isTouchDevice(): boolean {
		return (
			typeof window !== 'undefined' && ('ontouchstart' in window || navigator.maxTouchPoints > 0)
		);
	}

	onMount(() => {
		if (wsListEl && !isTouchDevice()) {
			sortable = Sortable.create(wsListEl, {
				animation: 150,
				ghostClass: 'opacity-30',
				dragClass: 'cursor-grabbing',
				direction: 'vertical',
				onEnd: (evt) => {
					if (evt.oldIndex != null && evt.newIndex != null && evt.oldIndex !== evt.newIndex) {
						reorderWorkspaces(evt.oldIndex, evt.newIndex);
					}
				}
			});
		}

		unbindSocketListener = socketStore.on('events:chat', handleChatEvent);
	});

	onDestroy(() => {
		sortable?.destroy();
		unbindSocketListener?.();
		unbindSocketListener = null;
	});
</script>

<div class="flex items-center justify-between h-8 pl-3.5 pr-1.5 shrink-0">
	<button
		class="group flex flex-1 h-full items-center gap-1 text-left text-xs text-gray-400 hover:text-gray-500 dark:text-gray-500 dark:hover:text-gray-400 transition-colors duration-100"
		onclick={() => (workspacesExpanded = !workspacesExpanded)}
		aria-expanded={workspacesExpanded}
		aria-controls="workspace-list"
	>
		<span>{$t('sidebar.workspaces')}</span>
		<span
			class="flex opacity-0 group-hover:opacity-100 transition-all duration-100"
			style="transform: rotate({workspacesExpanded ? '90deg' : '0deg'})"
		>
			<Icon name="chevron-right" size={11} />
		</span>
	</button>
	<button
		class="flex items-center justify-center w-7 h-7 rounded-lg text-gray-300 hover:text-gray-500 dark:text-gray-600 dark:hover:text-gray-400 transition-colors duration-100"
		onclick={onaddworkspace}
		aria-label={$t('sidebar.addWorkspace')}
		use:tooltip={$t('sidebar.addWorkspace')}
	>
		<Icon name="plus" size={14} />
	</button>
</div>

<div
	id="workspace-list"
	bind:this={wsListEl}
	class="flex-1 overflow-y-auto px-1.5"
	class:invisible={!workspacesExpanded}
>
	{#each $workspaceList as ws (ws.path)}
		{@const isExpanded = expandedWorkspaces.has(ws.path)}
		{@const chats = wsChatsCache.get(ws.path)}
		{@const hasMoreChats = wsChatsHasMore.get(ws.path)}
		{@const isLoading = wsChatsLoading.has(ws.path)}
		<div class="ws-item">
			<div
				class="group flex items-center gap-1 w-full h-7 px-2 rounded-lg text-xs font-medium transition-colors duration-100
				{ws.path === currentPath
					? 'bg-gray-200/50 text-gray-900 dark:bg-white/8 dark:text-white'
					: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
			>
				<a
					href="/?workspace={encodeURIComponent(ws.path)}"
					class="flex items-center gap-1.5 flex-1 min-w-0 no-underline text-inherit"
					onclick={(e) => openWorkspace(e, ws.path)}
				>
					{#if $chatEnabled}
						<span
							class="ws-chevron shrink-0"
							role="button"
							tabindex="0"
							onclick={(e) => {
								e.stopPropagation();
								e.preventDefault();
								toggleWorkspaceExpand(ws.path);
							}}
							onkeydown={(e) => {
								if (e.key === 'Enter' || e.key === ' ') {
									e.preventDefault();
									toggleWorkspaceExpand(ws.path);
								}
							}}
							aria-label={isExpanded ? 'Collapse chats' : 'Show chats'}
							aria-expanded={isExpanded}
						>
							<span style="transform: rotate({isExpanded ? '90deg' : '0deg'})" class="flex">
								<Icon name="chevron-right" size={11} />
							</span>
						</span>
					{/if}
					<Icon name="folder" size={14} />
					<span class="min-w-0 truncate text-left">{ws.name}</span>
					{#if ws.unread_count > 0}
						<span
							class="inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-md bg-sky-500/10 px-1 text-[0.625rem] font-semibold text-sky-600 dark:bg-sky-400/10 dark:text-sky-300"
						>
							{new Intl.NumberFormat(undefined, {
								notation: 'compact',
								compactDisplay: 'short'
							}).format(ws.unread_count)}
						</span>
					{/if}
				</a>
				<span
					class="flex items-center justify-center w-4 h-4 shrink-0 text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-600 dark:hover:text-gray-300 transition-all duration-75"
					role="button"
					tabindex="-1"
					onclick={(e) => openWsMenu(e, ws.path)}
					aria-label={$t('sidebar.workspaceOptions')}
				>
					<Icon name="three-dots" size={11} />
				</span>
				{#if $chatEnabled}
					<span
						class="flex items-center justify-center w-4 h-4 shrink-0 text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-600 dark:hover:text-gray-300 transition-all duration-75"
						role="button"
						tabindex="-1"
						onclick={() => newChat(ws.path)}
						aria-label={$t('bar.newChat')}
						use:tooltip={$t('bar.newChat')}
					>
						<Icon name="pencil" size={11} />
					</span>
				{/if}
			</div>

			{#if $chatEnabled && isExpanded}
				<div class="ws-chats">
					{#if isLoading && !chats}
						<div class="ws-chat-loading">
							<span class="ws-chat-loading-dot"></span>
							<span class="ws-chat-loading-dot"></span>
							<span class="ws-chat-loading-dot"></span>
						</div>
					{:else if (chats && chats.length > 0) || (ws.path === currentPath && openChats.length > 0)}
						<!-- One stable order (most recent first): opening a chat marks its row
						     in place instead of moving it. Only open chats that are not in the
						     list yet (new chats, older pages) go on top. -->
						{#if ws.path === currentPath}
							{@const listedIds = new Set((chats ?? []).map((c) => c.id))}
							{@const unlisted = openChats.filter((o) => !listedIds.has(o.chat.id))}
							{#each unlisted as o (o.chat.id)}
								{@render chatRow(o.chat, ws.path, o)}
							{/each}
							{#if unlisted.length && (chats ?? []).length}
								<div class="ws-chats-divider"></div>
							{/if}
						{/if}
						{#each chats ?? [] as chat (chat.id)}
							{@render chatRow(
								chat,
								ws.path,
								ws.path === currentPath
									? (openChats.find((o) => o.chat.id === chat.id) ?? null)
									: null
							)}
						{/each}
						{#if hasMoreChats}
							<button
								class="ws-chat-show-more"
								disabled={isLoading}
								onclick={() => fetchWorkspaceChats(ws.path, true)}
							>
								{$t('sidebar.showMore')}
							</button>
						{/if}
					{/if}
				</div>
			{/if}
		</div>
	{/each}

	{#if $workspaceList.length === 0}
		<div class="flex flex-col items-center justify-center py-12">
			<p class="text-xs text-gray-400 dark:text-gray-600">{$t('sidebar.noWorkspaces')}</p>
		</div>
	{/if}
</div>

{#snippet chatRow(chat: ChatInfo, wsPath: string, open: OpenChat | null)}
	{@const agents = subagentsOf(chat.id)}
	<ChatItem
		{chat}
		isSelected={chat.id === currentChatId}
		isOpen={!!open}
		badge={badgeFor(chat)}
		onclick={() => openChat(chat.id, wsPath)}
		onclose={open ? () => closeOpenChat(open) : undefined}
		onmenu={isDraftId(chat.id) ? undefined : (e) => openChatMenu(e, chat.id, wsPath)}
	/>
	{#if agents.length}
		{@const branchOpen = agentBranchOpen(chat.id)}
		{@const working = agents.filter((a) => a.status === 'running' || a.status === 'queued').length}
		<div class="ws-agents">
			<button
				type="button"
				class="ws-agents-toggle"
				aria-expanded={branchOpen}
				onclick={() => (agentBranches = { ...agentBranches, [chat.id]: !branchOpen })}
			>
				<span style="transform: rotate({branchOpen ? '90deg' : '0deg'})" class="flex">
					<Icon name="chevron-right" size={9} />
				</span>
				<span>Agents</span>
				<span class="ws-agents-count">
					{working ? `${working} working` : `${agents.length}`}
				</span>
			</button>
			{#if branchOpen}
				{#each agents as agent, i (agent.chat_id)}
					<button
						type="button"
						class="ws-agent"
						title={`${agent.task}\n${describeActivity(agent)}`}
						onclick={() => openSubagent(chat.id, wsPath, agent.call_id)}
					>
						<span class="ws-agent-dot ws-agent-{agent.status}"></span>
						<span class="truncate">Agent {i + 1}: {agent.task}</span>
					</button>
				{/each}
			{/if}
		</div>
	{/if}
{/snippet}

{#if wsMenuPath && wsMenuAnchor}
	<DropdownMenu
		anchor={wsMenuAnchor}
		items={[
			{
				label: $t('bar.agentsDashboard'),
				icon: 'list',
				onclick: () => handleOpenAgents(wsMenuPath!)
			},
			{
				label: $t('sidebar.remove'),
				icon: 'xmark',
				onclick: () => handleRemoveWorkspace(wsMenuPath!)
			}
		]}
		onclose={closeWsMenu}
	/>
{/if}

{#if chatMenu}
	<DropdownMenu
		anchor={chatMenu.anchor}
		align="end"
		items={[
			{
				label: 'Open beside',
				icon: 'split-view',
				onclick: openChatBeside
			},
			{
				label: $t('files.copyPath'),
				icon: 'copy',
				onclick: copyChatPath
			},
			{
				label: $t('files.rename'),
				icon: 'pencil',
				onclick: handleRenameChat
			},
			{
				label: $t('chat.history.delete'),
				icon: 'trash',
				onclick: handleDeleteChat
			}
		]}
		onclose={closeChatMenu}
	/>
{/if}

<style>
	@reference "../../app.css";

	.ws-item {
		margin-bottom: 0.125rem;
	}

	.ws-chevron {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 0.875rem;
		height: 0.875rem;
		border-radius: 0.25rem;
		color: var(--app-fg-muted, #9ca3af);
	}

	.ws-chevron:hover {
		color: var(--app-fg, inherit);
		background: var(--app-hover, rgba(127, 127, 127, 0.12));
	}

	.ws-agents {
		margin-left: 0.75rem;
		border-left: 1px solid var(--app-border, rgba(127, 127, 127, 0.2));
		padding-left: 0.25rem;
	}

	.ws-agents-toggle,
	.ws-agent {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		width: 100%;
		height: 1.375rem;
		padding: 0 0.375rem;
		border-radius: 0.375rem;
		font-size: 0.6875rem;
		color: var(--app-fg-muted, #9ca3af);
		text-align: left;
	}

	.ws-agents-toggle:hover,
	.ws-agent:hover {
		background: var(--app-hover, rgba(127, 127, 127, 0.08));
		color: var(--app-fg, inherit);
	}

	.ws-agents-count {
		margin-left: auto;
		font-variant-numeric: tabular-nums;
	}

	.ws-agent-dot {
		width: 0.375rem;
		height: 0.375rem;
		border-radius: 9999px;
		flex-shrink: 0;
		background: #9ca3af;
	}

	.ws-agent-running {
		background: #3b82f6;
		animation: ws-agent-pulse 1.4s ease-in-out infinite;
	}

	.ws-agent-done {
		background: #10b981;
	}

	.ws-agent-failed {
		background: #ef4444;
	}

	.ws-agent-stopped {
		background: #f59e0b;
	}

	@keyframes ws-agent-pulse {
		50% {
			opacity: 0.35;
		}
	}

	.ws-chats {
		margin-top: 0.125rem;
		margin-left: 0.875rem;
		padding-left: 0.25rem;
		padding-bottom: 0.25rem;
		border-left: 1px solid var(--app-border, rgba(127, 127, 127, 0.2));
	}

	.ws-chats-divider {
		height: 1px;
		margin: 0.25rem 0.5rem;
		background: var(--app-border, rgba(127, 127, 127, 0.2));
	}

	.ws-chat-show-more {
		display: block;
		width: 100%;
		padding: 0.125rem 0.5rem;
		border: none;
		background: none;
		cursor: pointer;
		font-size: 0.6875rem;
		color: var(--app-fg-subtle);
		text-align: left;
		transition: color 0.1s;
	}

	.ws-chat-show-more:hover {
		color: var(--app-fg);
	}

	.ws-chat-loading {
		display: flex;
		gap: 0.25rem;
		padding: 0.375rem 0.5rem;
	}

	.ws-chat-loading-dot {
		width: 0.25rem;
		height: 0.25rem;
		border-radius: 50%;
		background: var(--app-fg-subtle);
		animation: dotPulse 1s ease-in-out infinite;
	}

	:global(.dark) .ws-chat-loading-dot {
		background: var(--app-fg-muted);
	}

	.ws-chat-loading-dot:nth-child(2) {
		animation-delay: 0.15s;
	}

	.ws-chat-loading-dot:nth-child(3) {
		animation-delay: 0.3s;
	}

	@keyframes dotPulse {
		0%,
		100% {
			opacity: 0.3;
		}
		50% {
			opacity: 1;
		}
	}
</style>
