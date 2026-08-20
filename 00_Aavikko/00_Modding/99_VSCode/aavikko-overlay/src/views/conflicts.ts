import * as vscode from 'vscode';
import { ConflictEntry, StateManager } from '../state';

/** Conflicts view — driven by StateManager (Status.py / Check.py data). */

export interface ConflictData {
    type: 'patch' | 'mod';
    path: string;
    note?: string;
}

export class ConflictItem extends vscode.TreeItem {
    constructor(readonly data: ConflictData) {
        super(data.path, vscode.TreeItemCollapsibleState.None);
        const isPatch = data.type === 'patch';
        const maxLen = 20;
        const noteStr = data.note
            ? ` · ${data.note.length > maxLen ? data.note.substring(0, maxLen) + '…' : data.note}`
            : '';
        this.description = `${isPatch ? 'patch' : 'mod'}${noteStr}`;
        this.tooltip = `${isPatch ? 'Patches/' : 'Mods/'} conflict\n` +
            `Path: ${data.path}\n` +
            (data.note ? `Note: ${data.note}\n` : '') +
            `Click to open diff view`;
        this.iconPath = new vscode.ThemeIcon(isPatch ? 'diff' : 'sparkle');
        this.contextValue = `conflict_${data.type}`;
        this.command = {
            command: 'aavikko.openConflictDiff',
            title: 'Open Diff',
            arguments: [data],
        };
    }
}

class CategoryItem extends vscode.TreeItem {
    constructor(label: string, icon: string, readonly children: ConflictItem[]) {
        super(label, vscode.TreeItemCollapsibleState.Expanded);
        this.iconPath = new vscode.ThemeIcon(icon);
    }
}

export class ConflictTreeProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private readonly _onDidChange = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData = this._onDidChange.event;

    constructor(private readonly state: StateManager) {
        state.onDidChange(() => this._onDidChange.fire());
    }

    refresh(): void {
        this._onDidChange.fire();
    }

    getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
        return element;
    }

    getChildren(element?: vscode.TreeItem): vscode.TreeItem[] {
        if (element instanceof CategoryItem) {
            return element.children;
        }
        if (element) {
            return [];
        }
        const st = this.state.current;
        const items: vscode.TreeItem[] = [];

        if (!st.baseline_recorded) {
            const noBaseline = new vscode.TreeItem(
                'No baseline — run Check.py first', vscode.TreeItemCollapsibleState.None);
            noBaseline.iconPath = new vscode.ThemeIcon('warning');
            noBaseline.command = { command: 'aavikko.runCheck', title: 'Run Check' };
            items.push(noBaseline);
            return items;
        }

        // Filter out already-resolved conflicts
        const unresolvedPatches = st.conflicts.patches.filter(c => {
            const d = st.decisions.patches[c.path]?.decision;
            return d !== 'fr' && d !== 'u' && d !== 's';
        });
        const unresolvedMods = st.conflicts.mods.filter(c => {
            const d = st.decisions.mods[c.path]?.decision;
            return d !== 'k' && d !== 'r';
        });

        if (unresolvedPatches.length) {
            items.push(new CategoryItem(
                `Patches/ conflicts (${unresolvedPatches.length})`, 'diff',
                unresolvedPatches.map(c => new ConflictItem(
                    { type: 'patch', path: c.path, note: c.note }))));
        }
        if (unresolvedMods.length) {
            items.push(new CategoryItem(
                `Mods/ conflicts (${unresolvedMods.length})`, 'sparkle',
                unresolvedMods.map(c => new ConflictItem(
                    { type: 'mod', path: c.path, note: c.note }))));
        }
        if (!items.length) {
            const okItem = new vscode.TreeItem(
                'No conflicts — all resolved', vscode.TreeItemCollapsibleState.None);
            okItem.iconPath = new vscode.ThemeIcon('check');
            items.push(okItem);
        }
        return items;
    }
}

/** Accepts whatever a menu/command passes and normalizes to ConflictData. */
export function extractConflictData(arg: unknown): ConflictData | null {
    if (!arg) { return null; }
    if (arg instanceof ConflictItem) { return arg.data; }
    if (typeof arg === 'object' && arg !== null) {
        const o = arg as Record<string, unknown>;
        if (o.data && typeof o.data === 'object') {
            return (o.data as ConflictItem['data']);
        }
        if (typeof o.path === 'string') {
            return {
                type: (o.type as 'patch' | 'mod') ?? 'patch',
                path: o.path,
                note: typeof o.note === 'string' ? o.note : undefined,
            };
        }
    }
    if (typeof arg === 'string') {
        return { type: 'patch', path: arg };
    }
    return null;
}