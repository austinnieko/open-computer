import { get, writable } from 'svelte/store';

export interface ConfirmOptions {
	title: string;
	message: string;
	confirmLabel?: string;
	cancelLabel?: string;
}

/** A third, secondary answer (e.g. "No, abandon it") shown between Cancel and Confirm. */
export interface ChoiceOptions extends ConfirmOptions {
	altLabel: string;
	/** Style the alternative as destructive. */
	altDanger?: boolean;
}

export type ConfirmAnswer = 'confirm' | 'alt' | 'cancel';

export interface ConfirmRequest extends Required<ConfirmOptions> {
	id: number;
	altLabel: string | null;
	altDanger: boolean;
	resolve: (answer: ConfirmAnswer) => void;
}

export const confirmDialog = writable<ConfirmRequest | null>(null);

let nextId = 0;

function open(options: ConfirmOptions & Partial<ChoiceOptions>): Promise<ConfirmAnswer> {
	if (typeof window === 'undefined') return Promise.resolve('cancel');

	const existing = get(confirmDialog);
	if (existing) existing.resolve('cancel');

	return new Promise((resolve) => {
		confirmDialog.set({
			id: ++nextId,
			title: options.title,
			message: options.message,
			confirmLabel: options.confirmLabel ?? 'Confirm',
			cancelLabel: options.cancelLabel ?? 'Cancel',
			altLabel: options.altLabel ?? null,
			altDanger: options.altDanger ?? false,
			resolve
		});
	});
}

export async function requestConfirm(options: ConfirmOptions): Promise<boolean> {
	return (await open(options)) === 'confirm';
}

/** Ask with three answers. Escape and clicking outside always mean 'cancel'. */
export function requestChoice(options: ChoiceOptions): Promise<ConfirmAnswer> {
	return open(options);
}

export function settleConfirm(answer: boolean | ConfirmAnswer): void {
	const request = get(confirmDialog);
	if (!request) return;
	confirmDialog.set(null);
	request.resolve(answer === true ? 'confirm' : answer === false ? 'cancel' : answer);
}
