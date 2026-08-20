import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';
import { log, logError } from './logger';

/**
 * Python bridge — runs Patcher scripts safely.
 *
 * Key improvements over v0.1:
 *  - execFile with argv arrays: no shell, no quoting/injection bugs
 *  - python3 → python → py auto-detection (Windows support)
 *  - cached interpreter lookup, invalidated on settings change
 */

export interface RunResult {
    stdout: string;
    stderr: string;
    code: number;
}

let cachedPython: string | null | undefined;

export function invalidatePythonCache(): void {
    cachedPython = undefined;
}

async function probe(cmd: string): Promise<boolean> {
    return new Promise((resolve) => {
        cp.execFile(cmd, ['--version'], { timeout: 5000 }, (err) => resolve(!err));
    });
}

/** Resolve the Python interpreter: setting → python3 → python → py. */
export async function getPython(): Promise<string | null> {
    if (cachedPython !== undefined) {
        return cachedPython;
    }
    const configured = vscode.workspace.getConfiguration('aavikko').get<string>('pythonPath', '');
    if (configured) {
        if (await probe(configured)) {
            return (cachedPython = configured);
        }
        logError(`Configured aavikko.pythonPath "${configured}" does not run; falling back to auto-detect`);
    }
    for (const candidate of ['python3', 'python', 'py']) {
        if (await probe(candidate)) {
            log(`Python interpreter: ${candidate}`);
            return (cachedPython = candidate);
        }
    }
    cachedPython = null;
    return null;
}

/** Run a Patcher script non-interactively. Returns null if Python missing. */
export async function runScript(
    patcherDir: string,
    script: string,
    args: string[] = [],
    timeoutMs = 120_000,
): Promise<RunResult | null> {
    const python = await getPython();
    if (!python) {
        vscode.window.showErrorMessage(
            'Aavikko: Python not found. Install Python 3 or set aavikko.pythonPath.');
        return null;
    }
    log(`$ ${python} -X utf8 ${script} ${args.join(' ')}  (cwd: ${patcherDir})`);
    return new Promise((resolve) => {
        cp.execFile(
            python,
            // -X utf8 forces UTF-8 mode in Python — critical on Windows where
            // the default encoding is cp1251 (can't encode →, —, ✓, ⚠).
            // Without this, scripts crash with UnicodeEncodeError.
            ['-X', 'utf8', path.join(patcherDir, script), ...args],
            { cwd: patcherDir, timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024 },
            (err, stdout, stderr) => {
                const code = err && typeof err.code === 'number' ? err.code : 0;
                resolve({ stdout: stdout ?? '', stderr: stderr ?? '', code });
            },
        );
    });
}

/**
 * Run a Patcher script in the integrated terminal (for interactive scripts
 * like Check.py with prompts). Reuses a single "Aavikko" terminal.
 *
 * Cross-platform shell handling:
 *   - On Windows, VS Code's default terminal is PowerShell, which does NOT
 *     accept `&&` as a command separator (it's a bash-only operator).
 *     PowerShell uses `;` instead.
 *   - On macOS/Linux, the default is bash/zsh, which DOES accept `&&`.
 *   - We can't reliably detect the active shell profile, so we use a
 *     PowerShell-compatible sequence that also works in bash:
 *       cd "DIR"; COMMAND
 *     Both shells accept `;` as a statement separator.
 *   - As a bonus: we explicitly set cwd via the `cd` command rather than
 *     relying on terminal.sendText's cwd option (which doesn't exist for
 *     existing terminals — only for newly-created ones via createTerminal).
 */
let terminal: vscode.Terminal | null = null;

export function runScriptInTerminal(patcherDir: string, commandLine: string): void {
    if (terminal && !vscode.window.terminals.includes(terminal)) {
        terminal = null; // closed by user
    }
    if (!terminal) {
        // Create terminal with explicit cwd so we don't need `cd` at all.
        // This works on all shells (PowerShell, bash, zsh, cmd) — the shell
        // starts in the patcherDir, and the command runs there directly.
        terminal = vscode.window.createTerminal({
            name: 'Aavikko',
            cwd: patcherDir,
        });
    }
    terminal.show(true);
    // Just send the command — no `cd ... &&` prefix needed because we set
    // cwd when creating the terminal. Works in PowerShell, bash, zsh, cmd.
    terminal.sendText(commandLine);
}

export function disposeTerminal(): void {
    terminal?.dispose();
    terminal = null;
}