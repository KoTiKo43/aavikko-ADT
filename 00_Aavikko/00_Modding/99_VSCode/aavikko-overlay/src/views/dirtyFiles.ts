import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { DirtyEntry, StateManager } from '../state';

/**
 * Dirty Files — upstream files changed since HEAD, not yet captured.
 *
 * v0.2: source of truth is `git status --porcelain` (via Status.py) instead of
 * v0.1's mtime scan — correct after checkouts/touches, shows untracked files,
 * works for RobustToolbox too, and doesn't require overlay to be applied.
 *
 * v0.2.2: added "Recent Changes" group for files WITHOUT overlay file yet
 *         (has_overlay=false). Those are the most actionable — dev just made
 *         a change and hasn't captured it yet. Other groups collapsed by default.
 */

export class DirtyFileItem extends vscode.TreeItem {
    constructor(
        readonly buildRoot: string,
        readonly entry: DirtyEntry,
    ) {
        super(entry.path, vscode.TreeItemCollapsibleState.None);
        this.filePath = path.join(buildRoot, entry.path);
        this.resourceUri = vscode.Uri.file(this.filePath);
        this.description = entry.status === '??' ? 'new' : 'modified';

        let stateIcon: string;
        let stateLabel: string;
        if (entry.has_overlay) {
            stateIcon = entry.type === 'resources' ? 'package' : 'edit';
            stateLabel = entry.type === 'resources' ? 'Copy exists in overlay' : 'Patch exists';
        } else {
            stateIcon = 'sparkle';
            stateLabel = 'New (no overlay file yet)';
        }
        this.iconPath = new vscode.ThemeIcon(stateIcon);

        const typeLabel = entry.type === 'resources'
            ? 'Resources (direct copy)'
            : entry.type === 'robust'
                ? 'RobustToolbox (.cs.patch)'
                : 'Content (.cs.patch)';
        let mtime = '';
        try {
            mtime = `\nModified: ${formatTimeAgo(fs.statSync(this.filePath).mtimeMs)}`;
        } catch { /* file may be gone */ }
        this.tooltip = `${entry.path}\nState: ${stateLabel}\nType: ${typeLabel}${mtime}`;

        this.contextValue = entry.has_overlay ? 'dirtyFileWithPatch' : 'dirtyFileNew';
        this.command = {
            command: 'aavikko.openDirtyFile',
            title: 'Open File',
            arguments: [this.filePath],
        };
    }

    readonly filePath: string;
}

class GroupItem extends vscode.TreeItem {
    constructor(
        label: string,
        readonly children: DirtyFileItem[],
        icon: string,
        tooltip: string,
        collapsedByDefault: boolean,
    ) {
        super(
            label,
            collapsedByDefault
                ? vscode.TreeItemCollapsibleState.Collapsed
                : (children.length
                    ? vscode.TreeItemCollapsibleState.Expanded
                    : vscode.TreeItemCollapsibleState.None),
        );
        this.iconPath = new vscode.ThemeIcon(icon);
        this.description = `${children.length}`;
        this.tooltip = tooltip;
    }
}

export class DirtyFilesProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private readonly _onDidChange = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData = this._onDidChange.event;

    constructor(
        private readonly buildRoot: string,
        private readonly state: StateManager,
    ) {
        state.onDidChange(() => this._onDidChange.fire());
    }

    refresh(): void {
        this._onDidChange.fire();
    }

    getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
        return element;
    }

    getChildren(element?: vscode.TreeItem): vscode.TreeItem[] {
        if (element instanceof GroupItem) {
            return element.children;
        }
        if (element) {
            return [];
        }
        const st = this.state.current;
        const dirty = st.dirty;
        if (!dirty.length) {
            return [];
        }

        // "Recent Changes" = files modified AFTER the Apply timestamp.
        //
        // Why not just `!entry.has_overlay`? Because after Apply.py:
        //   - Files WITH overlay are also "modified" in git (vs HEAD) since overlay
        //     overwrote the upstream file. They show up as `M` in `git status`.
        //   - If dev just ran Apply and hasn't touched anything, we don't want ALL
        //     overlay-tracked files to flood "Recent Changes".
        //   - We want "Recent Changes" to mean: "files YOU edited after the last Apply"
        //
        // So we compare each file's mtime to applyTime. If overlay is NOT applied
        // (pristine state) — every file with mtime > 0 qualifies, which is too broad.
        // In that case we fall back to: "any dirty file is recent" (because dev is
        // editing pristine upstream without overlay).
        const appliedAt = st.applied?.at;
        const applyTime = appliedAt ? new Date(appliedAt).getTime() : 0;

        const isOverlayApplied = st.state === 'applied';

        const recent: DirtyEntry[] = [];
        const content: DirtyEntry[] = [];
        const resources: DirtyEntry[] = [];
        const robust: DirtyEntry[] = [];

        for (const entry of dirty) {
            const fullPath = path.join(this.buildRoot, entry.path);
            const mtime = mtimeSafe(fullPath);

            // File qualifies for "Recent Changes" if:
            //   - Overlay IS applied: file was touched AFTER Apply (mtime > applyTime)
            //   - Overlay NOT applied: any dirty file qualifies (dev editing pristine)
            const isRecent = isOverlayApplied
                ? (applyTime > 0 && mtime > applyTime)
                : true;

            if (isRecent) {
                recent.push(entry);
            } else if (entry.type === 'resources') {
                resources.push(entry);
            } else if (entry.type === 'robust') {
                robust.push(entry);
            } else {
                content.push(entry);
            }
        }

        const sortByMtimeDesc = (entries: DirtyEntry[]): DirtyFileItem[] =>
            entries
                .map(e => new DirtyFileItem(this.buildRoot, e))
                .sort((a, b) => mtimeSafe(b.filePath) - mtimeSafe(a.filePath));

        const items: vscode.TreeItem[] = [];

        // Recent Changes — expanded by default (most actionable)
        if (recent.length > 0) {
            const hint = isOverlayApplied
                ? `Files YOU edited after Apply (mtime > applyTime).\n` +
                  `Action required: open each file, then click the inline ✏ button (Generate Patch)\n` +
                  `to save your changes to Aavikko.* overlay.\n\n` +
                  `${recent.length} file(s)`
                : `Overlay is NOT applied. ALL modified files shown here.\n` +
                  `⚠ WARNING: you are editing pristine upstream — changes will be lost!\n` +
                  `Run Apply.py first, then re-edit.\n\n` +
                  `${recent.length} file(s)`;
            items.push(new GroupItem(
                'Recent Changes',
                sortByMtimeDesc(recent),
                'sparkle',
                hint,
                false,  // expanded by default
            ));
        }

        // Content — collapsed by default (already tracked, bulk files)
        if (content.length > 0) {
            items.push(new GroupItem(
                'Content',
                sortByMtimeDesc(content),
                'file-code',
                `Content.* files with existing overlay (already tracked).\n` +
                `${content.length} file(s) — expand to see them`,
                true,  // collapsed by default
            ));
        }

        // Resources — collapsed by default (already tracked, bulk files)
        if (resources.length > 0) {
            items.push(new GroupItem(
                'Resources',
                sortByMtimeDesc(resources),
                'file-media',
                `Resources/ files with existing overlay (already tracked).\n` +
                `${resources.length} file(s) — expand to see them`,
                true,  // collapsed by default
            ));
        }

        // RobustToolbox — collapsed by default
        if (robust.length > 0) {
            items.push(new GroupItem(
                'RobustToolbox',
                sortByMtimeDesc(robust),
                'gear',
                `RobustToolbox/ files with existing overlay (already tracked).\n` +
                `${robust.length} file(s) — expand to see them`,
                true,  // collapsed by default
            ));
        }

        return items;
    }
}

function mtimeSafe(p: string): number {
    try {
        return fs.statSync(p).mtimeMs;
    } catch {
        return 0;
    }
}

export function formatTimeAgo(mtimeMs: number): string {
    const diff = Date.now() - mtimeMs;
    if (diff < 60_000) { return 'just now'; }
    if (diff < 3_600_000) { return `${Math.floor(diff / 60_000)}m ago`; }
    if (diff < 86_400_000) { return `${Math.floor(diff / 3_600_000)}h ago`; }
    return `${Math.floor(diff / 86_400_000)}d ago`;
}