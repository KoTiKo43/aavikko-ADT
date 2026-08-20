import * as vscode from 'vscode';

/** Central output channel — replaces console.log noise. */
let channel: vscode.OutputChannel | undefined;

export function initLogger(): vscode.OutputChannel {
    if (!channel) {
        channel = vscode.window.createOutputChannel('Aavikko');
    }
    return channel;
}

export function log(message: string): void {
    const ts = new Date().toISOString().substring(11, 19);
    channel?.appendLine(`[${ts}] ${message}`);
}

export function logError(message: string, err?: unknown): void {
    const detail = err instanceof Error ? ` — ${err.message}` : (err ? ` — ${String(err)}` : '');
    log(`ERROR: ${message}${detail}`);
}

export function showLog(): void {
    channel?.show(true);
}