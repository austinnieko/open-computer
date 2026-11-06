/**
 * Chat API: send messages, approve/reject tools, cancel tasks, fetch chats.
 */
import { fetchJSON, jsonBody } from '$lib/apis';

export interface ChatMessageRow {
	id: string;
	parent_id: string | null;
	role: 'user' | 'assistant';
	content: string;
	model: string | null;
	done: boolean;
	output: any[] | null;
	usage: Record<string, number> | null;
	meta: Record<string, any> | null;
	created_at: number;
	/** Live only: connection/liveness of the agent run still generating this reply. */
	agent_status?: AgentRunStatus | null;
}

export interface AgentRunStatus {
	state: 'connected' | 'waiting' | 'retrying' | 'reconnecting' | 'unreachable';
	/** Epoch seconds of the agent's last real event. */
	last_activity?: number | null;
	detail?: string | null;
	attempt?: number | null;
}

export interface ChatInfo {
	id: string;
	title: string;
	summary: string | null;
	folder: string;
	meta: Record<string, any> | null;
	current_message_id: string | null;
	created_at: number;
	updated_at: number;
	last_read_at: number | null;
	is_active?: boolean;
}

export interface ContextUsage {
	tokens: number;
	estimated_tokens: number;
	threshold: number;
	percent: number;
}

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';

export interface ChatTask {
	id: string;
	content: string;
	status: TaskStatus;
}

export interface ChatDetail {
	chat: ChatInfo;
	messages: ChatMessageRow[];
	tasks?: ChatTask[];
	context_usage?: ContextUsage | null;
}

export interface SendMessageResult {
	chat_id: string;
	message_id: string;
	queued?: boolean;
	user_message?: ChatMessageRow;
	assistant_message?: ChatMessageRow;
}

export type ToolApprovalMode = 'ask' | 'auto' | 'full';

export interface ChatSendParams {
	tool_approval_mode?: ToolApprovalMode;
	plan_mode?: boolean;
	follow_edits?: boolean;
	request_params?: Record<string, unknown>;
	voice_mode?: boolean;
}

export interface CompactChatResult {
	ok: boolean;
	compacted: boolean;
	reason?: string;
	dropped_messages?: number;
	kept_messages?: number;
	summary_chars?: number;
	context_usage?: ContextUsage | null;
}

export interface UsageHeatmapEntry {
	date: string;
	tokens: number;
	messages: number;
	chats: number;
	models: Record<string, number>;
}

export interface UsageResponse {
	totals: {
		lifetime_tokens: number;
		peak_daily_tokens: number;
		longest_chat_seconds: number;
		current_streak: number;
		longest_streak: number;
		models_used: number;
		user_messages: number;
		assistant_messages: number;
		messages: number;
		total_chats: number;
	};
	insights: {
		average_tokens_per_chat: number;
		average_messages_per_active_day: number;
		user_message_share: number;
		assistant_message_share: number;
	};
	heatmap: UsageHeatmapEntry[];
	weekly_heatmap: UsageHeatmapEntry[];
	cumulative_heatmap: UsageHeatmapEntry[];
	top_models: { model_id: string; messages: number; total_tokens: number }[];
	top_tools: { name: string; count: number }[];
	period: { start_date: number; end_date: number; days: number };
}

// ── Queries ─────────────────────────────────────────────────

export const getChats = (
	workspace?: string,
	limit = 50,
	offset = 0,
	sortBy: 'title' | 'updated_at' = 'updated_at',
	sortDir: 'asc' | 'desc' = 'desc'
) =>
	fetchJSON<{ chats: ChatInfo[]; total: number; has_more: boolean }>(
		`/api/chats?${workspace ? `workspace=${encodeURIComponent(workspace)}&` : ''}limit=${limit}&offset=${offset}&sort_by=${sortBy}&sort_dir=${sortDir}`
	);

export const getChat = (chatId: string, modelId?: string) => {
	const suffix = modelId ? `?model_id=${encodeURIComponent(modelId)}` : '';
	return fetchJSON<ChatDetail>(`/api/chats/${chatId}${suffix}`);
};

export const getUsage = () => fetchJSON<UsageResponse>('/api/chats/usage');

export const deleteChat = (chatId: string) =>
	fetchJSON<{ ok: boolean }>(`/api/chats/${chatId}`, { method: 'DELETE' });

export const updateChatTitle = (chatId: string, title: string) =>
	fetchJSON<{ ok: boolean; title: string }>(`/api/chats/${chatId}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ title })
	});

export const updateChatSettings = (chatId: string, modelId: string, params: ChatSendParams) =>
	fetchJSON<{ ok: boolean }>(
		`/api/chats/${chatId}/settings`,
		// The backend route is PATCH; a POST here was rejected with 405, so chat
		// settings (plan mode, tool permissions, follow edits) never saved.
		{ ...jsonBody({ model_id: modelId, params }), method: 'PATCH' }
	);

export const forkChat = (chatId: string, messageId?: string | null) =>
	fetchJSON<{ ok: boolean; chat_id: string }>(
		`/api/chats/${chatId}/fork`,
		jsonBody({ message_id: messageId ?? null })
	);

// ── Mutations ───────────────────────────────────────────────

export const sendMessage = (
	content: string,
	modelId: string,
	workspace?: string,
	chatId?: string,
	parentId?: string | null,
	params: ChatSendParams = {},
	regenerationPrompt?: string,
	files?: { id: string; name: string; url: string; type: string }[]
) =>
	fetchJSON<SendMessageResult>(
		'/api/chats',
		jsonBody({
			content,
			model_id: modelId,
			...(workspace ? { workspace } : {}),
			chat_id: chatId,
			parent_id: parentId ?? null,
			regeneration_prompt: regenerationPrompt,
			files: files ?? [],
			params
		})
	);

export type ToolResolveAction = 'approve' | 'reject' | 'answer';

export const resolveToolCall = (
	chatId: string,
	messageId: string,
	callId: string,
	action: ToolResolveAction,
	options: { answers?: Record<string, string>; timedOut?: boolean } = {}
) =>
	fetchJSON(
		`/api/chats/${chatId}/messages/${messageId}/resolve`,
		jsonBody({
			call_id: callId,
			action,
			...(options.answers ? { answers: options.answers } : {}),
			...(options.timedOut ? { timed_out: options.timedOut } : {})
		})
	);

export const answerAskUser = (
	chatId: string,
	messageId: string,
	callId: string,
	answers: Record<string, string>,
	timedOut = false
) => resolveToolCall(chatId, messageId, callId, 'answer', { answers, timedOut });

export const cancelTask = (chatId: string, messageId: string) =>
	fetchJSON(`/api/chats/${chatId}/messages/${messageId}/cancel`, { method: 'POST' });

export const compactChat = (chatId: string, modelId?: string | null) =>
	fetchJSON<CompactChatResult>(
		`/api/chats/${chatId}/compact`,
		jsonBody({ model_id: modelId || null })
	);

export const updateCurrentMessage = (chatId: string, messageId: string) =>
	fetchJSON<{ ok: boolean }>(`/api/chats/${chatId}/current`, jsonBody({ message_id: messageId }));

export const updateMessage = (
	chatId: string,
	messageId: string,
	updates: { content?: string; output?: any[] }
) =>
	fetchJSON<{ ok: boolean }>(`/api/chats/${chatId}/messages/${messageId}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(updates)
	});

export const createMessage = (
	chatId: string,
	parentId: string | null,
	role: string,
	content: string,
	output?: any[]
) =>
	fetchJSON<{ ok: boolean; message_id: string }>(
		`/api/chats/${chatId}/messages`,
		jsonBody({ parent_id: parentId, role, content, output })
	);

// ── Queue management ────────────────────────────────────────

export const queueSendNow = (chatId: string, messageId: string) =>
	fetchJSON<{ ok: boolean; chat_id: string; message_id: string }>(
		`/api/chats/${chatId}/queue/${messageId}/send`,
		{ method: 'POST' }
	);

export const queueDelete = (chatId: string, messageId: string) =>
	fetchJSON<{ ok: boolean }>(`/api/chats/${chatId}/queue/${messageId}`, { method: 'DELETE' });

// ── Sub-agents ──────────────────────────────────────────────

export type SubagentStatus = 'queued' | 'running' | 'done' | 'failed' | 'stopped';

export interface SubagentActivity {
	kind: 'tool' | 'thinking' | 'writing';
	name?: string;
	status?: string;
	arguments?: Record<string, any>;
}

export interface SubagentInfo {
	chat_id: string;
	title: string;
	task: string;
	context: string;
	mode: 'foreground' | 'background';
	call_id: string | null;
	parent_message_id: string | null;
	delegation_id: string | null;
	status: SubagentStatus;
	started_at: number;
	updated_at: number;
	tool_calls: number;
	activity: SubagentActivity | null;
	result: string;
	model: string | null;
}

export const getSubagents = (chatId: string) =>
	fetchJSON<{ subagents: SubagentInfo[] }>(`/api/chats/${chatId}/subagents`);

export interface ChatStatsTotals {
	input: number;
	output: number;
	reasoning: number;
	cached: number;
	total: number;
	cost_usd: number;
	duration_ms: number;
}

export interface ChatStats {
	chat_id: string;
	created_at: number;
	updated_at: number;
	is_active: boolean;
	totals: ChatStatsTotals;
	tools: { name: string; count: number; failed: number }[];
	files: { path: string; edits: number }[];
	models: { id: string; replies: number }[];
	turns: {
		id: string;
		created_at: number;
		tokens: number;
		duration_ms: number;
		tool_calls: number;
		done: boolean;
	}[];
	user_messages: number;
	subagents: {
		count: number;
		running: number;
		tokens: number;
		cost_usd: number;
		tool_calls: number;
	};
	context_usage: ContextUsage | null;
}

/** Usage for one chat: tokens, time, tools, files and sub-agents. */
export const getChatStats = (chatId: string) => fetchJSON<ChatStats>(`/api/chats/${chatId}/stats`);
