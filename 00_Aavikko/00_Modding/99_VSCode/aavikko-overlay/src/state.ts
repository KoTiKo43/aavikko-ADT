import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { runScript } from './python';
import { log, logError } from './logger';

/**
 * Central state — single source of truth for the whole extension.
 *
 * Primary source: x03_Status.py --json (authoritative, computed by Python).
 * Fallback: .applied marker + git porcelain from JS (for older Patcher
 * versions without x03_Status.py).
 */

export interface GitInfo {
    branch: string;
    head: string;
    ahead?: number;
    behind?: number;
    robust: { head: string; branch: string } | null;
}

export interface AppliedInfo {
    at?: string;
    head_commit?: string;
    cs_patches_applied?: string[];
    robust_patches_applied?: string[];
    cs_patches_failed?: string[];
    cs_patches_skipped?: string[];
    counts?: Record<string, number>;
    corrupted?: boolean;
}

export interface DirtyEntry {
    path: string;
    status: string;
    type: 'resources' | 'content' | 'robust';
    has_overlay: boolean;
}

export interface ConflictEntry {
    path: string;
    note?: string;
    old_commit?: string;
    new_commit?: string;
    [key: string]: unknown;
}

export interface OverlayInventory {
    content_patches: string[];
    content_mods: string[];
    robust_patches: string[];
    robust_mods: string[];
    resource_patches: string[];
    resource_mods: string[];
}

export interface AavikkoStatus {
    state: 'applied' | 'pristine';
    git: GitInfo;
    applied: AppliedInfo | null;
    overlay: OverlayInventory;
    dirty: DirtyEntry[];
    conflicts: { patches: ConflictEntry[]; mods: ConflictEntry[] };
    decisions: { patches: Record<string, { decision?: string }>; mods: Record<string, { decision?: string }> };
    baseline_recorded: boolean;
}

const EMPTY_OVERLAY: OverlayInventory = {
    content_patches: [], content_mods: [],
    robust_patches: [], robust_mods: [],
    resource_patches: [], resource_mods: [],
};

const EMPTY_STATUS: AavikkoStatus = {
    state: 'pristine',
    git: { branch: '?', head: '', robust: null },
    applied: null,
    overlay: EMPTY_OVERLAY,
    dirty: [],
    conflicts: { patches: [], mods: [] },
    decisions: { patches: {}, mods: {} },
    baseline_recorded: false,
};

export class StateManager {
    private status: AavikkoStatus = EMPTY_STATUS;
    private readonly _onDidChange = new vscode.EventEmitter<AavikkoStatus>();
    readonly onDidChange = this._onDidChange.event;

    // Periodic poll fallback — runs every N seconds to catch state changes
    // that FileSystemWatcher might miss on network-mounted drives (Crucible,
    // NFS, FUSE, SMB/CIFS). Without this, switching VS Code tabs or focusing
    // other windows could leave stale state until a manual refresh.
    //
    // Polling is paused automatically while a long-running operation is in
    // progress (Apply/Clear/Generate take 30s-2min). See withBusy() below.
    private pollTimer: NodeJS.Timeout | null = null;
    private busyCount = 0;
    private static readonly POLL_INTERVAL_MS = 15000;  // 15 sec (user requested)
    private static readonly POLL_DEBOUNCE_MS = 1500;  // ignore rapid re-polls

    constructor(
        readonly buildRoot: string,
        readonly patcherDir: string,
    ) {
        this.startPolling();
    }

    /**
     * Mark state as "busy" — pauses polling while a long-running operation
     * (Apply/Clear/Generate) is executing. Returns a function to call when
     * the operation finishes (resumes polling + triggers immediate refresh).
     *
     * Usage:
     *   const done = state.withBusy();
     *   try { await apply(); } finally { done(); }
     */
    withBusy(): () => void {
        this.busyCount++;
        return () => {
            this.busyCount = Math.max(0, this.busyCount - 1);
            // Trigger immediate refresh after busy operation completes
            // (debounced so multiple done() calls don't trigger flood)
            setTimeout(() => { void this.refresh(); }, 200);
        };
    }

    private startPolling(): void {
        if (this.pollTimer) { return; }
        this.pollTimer = setInterval(() => {
            // Skip while busy (Apply/Clear/Generate running) — they take 30s-2min
            // and we'd just thrash Python subprocess if we polled during them.
            if (this.busyCount > 0) { return; }
            // Debounce: don't re-poll if a refresh was triggered <1.5s ago
            if (Date.now() - this.lastRefreshAt < StateManager.POLL_DEBOUNCE_MS) {
                return;
            }
            void this.refresh();
        }, StateManager.POLL_INTERVAL_MS);
    }

    private stopPolling(): void {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    }

    private lastRefreshAt = 0;

    get current(): AavikkoStatus {
        return this.status;
    }

    get applied(): boolean {
        return this.status.state === 'applied';
    }

    get appliedFilePath(): string {
        return path.join(this.patcherDir, '.applied');
    }

    get decisionsFilePath(): string {
        return path.join(this.patcherDir, '.conflict_decisions.yml');
    }

    /** Refresh status. Prefers x03_Status.py; falls back to built-in JS logic. */
    async refresh(): Promise<AavikkoStatus> {
        // Don't refresh if already refreshing (avoid stacking concurrent
        // x03_Status.py subprocesses — they each take 1-2s).
        if (this.refreshing) {
            return this.status;
        }
        this.refreshing = true;
        this.lastRefreshAt = Date.now();
        try {
            const statusPy = path.join(this.patcherDir, 'x03_Status.py');
            if (fs.existsSync(statusPy)) {
                const result = await runScript(this.patcherDir, 'x03_Status.py', ['--json'], 60_000);
                if (result && result.code === 0) {
                    try {
                        const parsed = JSON.parse(result.stdout.trim());
                        if (parsed.error) {
                            logError('x03_Status.py reported error', parsed.error);
                        } else {
                            this.status = this.normalize(parsed);
                            this._onDidChange.fire(this.status);
                            return this.status;
                        }
                    } catch (e) {
                        logError('Failed to parse x03_Status.py output', e);
                    }
                }
            }
            this.status = await this.fallbackStatus();
            this._onDidChange.fire(this.status);
            return this.status;
        } finally {
            this.refreshing = false;
        }
    }

    private refreshing = false;

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    private normalize(raw: any): AavikkoStatus {
        return {
            state: raw.state === 'applied' ? 'applied' : 'pristine',
            git: raw.git ?? EMPTY_STATUS.git,
            applied: raw.applied ?? null,
            overlay: { ...EMPTY_OVERLAY, ...(raw.overlay ?? {}) },
            dirty: Array.isArray(raw.dirty) ? raw.dirty : [],
            conflicts: {
                patches: raw.conflicts?.patches ?? [],
                mods: raw.conflicts?.mods ?? [],
            },
            decisions: {
                patches: raw.decisions?.patches ?? {},
                mods: raw.decisions?.mods ?? {},
            },
            baseline_recorded: !!raw.baseline_recorded,
        };
    }

    /**
     * Fallback for older Patcher without x03_Status.py:
     * .applied marker + git status --porcelain (root + RobustToolbox).
     */
    private async fallbackStatus(): Promise<AavikkoStatus> {
        log('x03_Status.py not found — using built-in fallback status');
        const applied = this.readAppliedMarker();
        const git = await this.fallbackGitInfo();
        const dirty = await this.fallbackDirty();
        const overlay = this.fallbackOverlayInventory();
        return {
            state: applied ? 'applied' : 'pristine',
            git,
            applied,
            overlay,
            dirty,
            conflicts: { patches: [], mods: [] },
            decisions: { patches: {}, mods: {} },
            baseline_recorded: fs.existsSync(path.join(this.patcherDir, '.upstream_state.json')),
        };
    }

    private readAppliedMarker(): AppliedInfo | null {
        try {
            if (!fs.existsSync(this.appliedFilePath)) {
                return null;
            }
            const data = JSON.parse(fs.readFileSync(this.appliedFilePath, 'utf-8'));
            return {
                at: data.applied_at,
                head_commit: data.head_commit,
                cs_patches_applied: data.cs_patches_applied ?? [],
                robust_patches_applied: data.robust_patches_applied ?? [],
                cs_patches_failed: data.cs_patches_failed ?? [],
                cs_patches_skipped: data.cs_patches_skipped ?? [],
                counts: {
                    patches_copied: data.patches_copied ?? 0,
                    mods_copied: data.mods_copied ?? 0,
                    content_mods_copied: data.content_mods_copied ?? 0,
                    robust_mods_copied: data.robust_mods_copied ?? 0,
                },
            };
        } catch {
            return { corrupted: true };
        }
    }

    private async fallbackGitInfo(): Promise<GitInfo> {
        const { execGit } = await import('./git');
        const branch = (await execGit(this.buildRoot, ['rev-parse', '--abbrev-ref', 'HEAD'])).trim() || '?';
        const head = (await execGit(this.buildRoot, ['rev-parse', 'HEAD'])).trim();
        const info: GitInfo = { branch, head, robust: null };
        const robustDir = path.join(this.buildRoot, 'RobustToolbox');
        if (fs.existsSync(path.join(robustDir, '.git'))) {
            const rbHead = (await execGit(robustDir, ['rev-parse', 'HEAD'])).trim();
            const rbBranch = (await execGit(robustDir, ['rev-parse', '--abbrev-ref', 'HEAD'])).trim();
            info.robust = { head: rbHead, branch: rbBranch };
        }
        return info;
    }

    private async fallbackDirty(): Promise<DirtyEntry[]> {
        const { execGit } = await import('./git');
        const dirty: DirtyEntry[] = [];
        const parse = (stdout: string, prefix = '') => {
            for (const line of stdout.split('\n')) {
                if (!line.trim()) { continue; }
                const parts = line.split(/\s+/);
                if (parts.length < 2) { continue; }
                const status = parts[0].trim();
                const p = parts.slice(1).join(' ').split(' -> ').pop()!.replace(/^"|"$/g, '');
                if (p.endsWith('/') || p.startsWith('Aavikko.') || p.startsWith('00_Aavikko/') || p.endsWith('.csproj')) { continue; }
                // Also skip files inside the Aavikko VS Code overlay source dir
                // (these are dev artifacts — .vsix, .ts, .js — not real game files)
                if (p.startsWith('00_Aavikko/00_Modding/99_VSCode/')) { continue; }
                if (!'MA?'.includes(status[0])) { continue; }
                if (prefix === '' && (p === 'RobustToolbox' || p.startsWith('RobustToolbox/'))) { continue; }
                const full = prefix + p;
                dirty.push({
                    path: full,
                    status,
                    type: full.startsWith('Resources/') ? 'resources'
                        : full.startsWith('RobustToolbox/') ? 'robust' : 'content',
                    has_overlay: false,
                });
            }
        };
        parse(await execGit(this.buildRoot, ['status', '--porcelain', '--untracked-files=all']));
        const robustDir = path.join(this.buildRoot, 'RobustToolbox');
        if (fs.existsSync(path.join(robustDir, '.git'))) {
            parse(await execGit(robustDir, ['status', '--porcelain', '--untracked-files=all']), 'RobustToolbox/');
        }
        return dirty;
    }

    private fallbackOverlayInventory(): OverlayInventory {
        const scan = (root: string, suffixes: string[] | null): string[] => {
            const out: string[] = [];
            const walk = (dir: string) => {
                let entries: fs.Dirent[];
                try {
                    entries = fs.readdirSync(dir, { withFileTypes: true });
                } catch {
                    return;
                }
                for (const e of entries) {
                    const full = path.join(dir, e.name);
                    if (e.isDirectory()) {
                        if (!e.name.startsWith('.') && !e.name.startsWith('@')) { walk(full); }
                    } else if (e.isFile()) {
                        if (e.name.startsWith('.') || e.name.startsWith('@') || e.name.endsWith('.broken')) { continue; }
                        if (suffixes && !suffixes.some(s => e.name.endsWith(s))) { continue; }
                        out.push(path.relative(root, full).replace(/\\/g, '/'));
                    }
                }
            };
            walk(root);
            return out.sort();
        };
        const at = (...segs: string[]) => path.join(this.buildRoot, ...segs);
        return {
            content_patches: scan(at('00_Aavikko/02_Content', 'Patches'), ['.patch']),
            content_mods: scan(at('00_Aavikko/02_Content', 'Mods'), ['.cs', '.xaml']),
            robust_patches: scan(at('00_Aavikko/03_RobustToolbox', 'Patches'), ['.patch']),
            robust_mods: scan(at('00_Aavikko/03_RobustToolbox', 'Mods'), ['.cs', '.xaml']),
            resource_patches: scan(at('00_Aavikko/01_Resources', 'Patches'), null),
            resource_mods: scan(at('00_Aavikko/01_Resources', 'Mods'), null),
        };
    }

    dispose(): void {
        this.stopPolling();
        this._onDidChange.dispose();
    }
}

// ── Shared path helpers ─────────────────────────────────────────────────────

/** Normalize a path relative to build root (forward slashes). */
export function relNorm(buildRoot: string, absPath: string): string {
    return path.relative(buildRoot, absPath).replace(/\\/g, '/').replace(/^\.\//, '');
}

export type OverlayType = 'resources' | 'robust' | 'content';

export function detectPathType(rel: string): OverlayType {
    if (rel.startsWith('Resources/')) { return 'resources'; }
    if (rel.startsWith('RobustToolbox/')) { return 'robust'; }
    return 'content';
}