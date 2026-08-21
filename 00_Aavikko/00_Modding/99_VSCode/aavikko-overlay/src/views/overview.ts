import * as vscode from 'vscode';
import { StateManager } from '../state';

/**
 * Overview — new in v0.2.
 * Single glance: overlay state, git info, overlay counts, quick actions.
 */

class InfoItem extends vscode.TreeItem {
    constructor(label: string, description: string, icon: string, tooltip?: string) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.description = description;
        this.iconPath = new vscode.ThemeIcon(icon);
        this.tooltip = tooltip ?? `${label}: ${description}`;
    }
}

class ActionItem extends vscode.TreeItem {
    constructor(label: string, icon: string, command: string, contextValue: string, tooltip: string) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.iconPath = new vscode.ThemeIcon(icon);
        this.tooltip = tooltip;
        this.contextValue = contextValue;
        this.command = { command, title: label };
    }
}

export class OverviewProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
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

    getChildren(): vscode.TreeItem[] {
        const st = this.state.current;
        const items: vscode.TreeItem[] = [];

        // ── State ──
        if (st.state === 'applied') {
            const appliedCount =
                (st.applied?.cs_patches_applied?.length ?? 0) +
                (st.applied?.robust_patches_applied?.length ?? 0);
            const stateItem = new InfoItem('Overlay applied', `${appliedCount} patches`, 'check-all',
                `Applied at ${st.applied?.at ?? '?'}\nHEAD: ${st.applied?.head_commit ?? '?'}`);
            stateItem.contextValue = 'state_applied';
            items.push(stateItem);
            items.push(new ActionItem('Clear Overlay', 'discard', 'aavikko.clear', 'action_clear',
                'Revert upstream to pristine state (x02_Clear.py)'));
        } else {
            const stateItem = new InfoItem('Upstream pristine', 'not applied', 'circle-outline',
                'Overlay is not applied — upstream files are untouched');
            stateItem.contextValue = 'state_pristine';
            items.push(stateItem);
            items.push(new ActionItem('Apply Overlay', 'check', 'aavikko.apply', 'action_apply',
                'Apply patches + copy mods into upstream (x00_Apply.py)'));
        }

        // ── Git ──
        const shortHead = st.git.head ? st.git.head.substring(0, 8) : '?';
        let gitDesc = shortHead;
        if (st.git.ahead !== undefined && st.git.behind !== undefined) {
            const parts: string[] = [];
            if (st.git.behind > 0) { parts.push(`↓${st.git.behind}`); }
            if (st.git.ahead > 0) { parts.push(`↑${st.git.ahead}`); }
            if (parts.length) { gitDesc += `  ${parts.join(' ')}`; }
        }
        items.push(new InfoItem(`Git: ${st.git.branch}`, gitDesc, 'git-branch',
            `HEAD: ${st.git.head}` +
            (st.git.robust ? `\nRobustToolbox: ${st.git.robust.head.substring(0, 8)} (${st.git.robust.branch})` : '')));

        // ── Overlay counts ──
        const patchesTotal =
            st.overlay.content_patches.length + st.overlay.robust_patches.length +
            st.overlay.resource_patches.length;
        const modsTotal =
            st.overlay.content_mods.length + st.overlay.robust_mods.length +
            st.overlay.resource_mods.length;
        items.push(new InfoItem('Overlay', `${patchesTotal} patches · ${modsTotal} mods`, 'package',
            `Content: ${st.overlay.content_patches.length}p / ${st.overlay.content_mods.length}m\n` +
            `RobustToolbox: ${st.overlay.robust_patches.length}p / ${st.overlay.robust_mods.length}m\n` +
            `Resources: ${st.overlay.resource_patches.length}p / ${st.overlay.resource_mods.length}m`));

        // ── Dirty / conflicts summary ──
        if (st.dirty.length > 0) {
            items.push(new InfoItem('Uncaptured changes', `${st.dirty.length}`, 'edit',
                'Modified upstream files not yet captured into overlay.\nRun Generate to capture them.'));
        }
        const conflicts =
            st.conflicts.patches.length + st.conflicts.mods.length;
        if (conflicts > 0) {
            items.push(new InfoItem('Unresolved conflicts', `${conflicts}`, 'warning',
                'See the Conflicts view below.'));
        }

        // ── Quick actions ──
        items.push(new ActionItem('Generate All Patches', 'diff', 'aavikko.generateAll', 'action',
            'Capture all modified upstream files into overlay (x01_Generate.py --all)'));
        items.push(new ActionItem('Check Conflicts', 'shield', 'aavikko.runCheck', 'action',
            'Detect upstream changes conflicting with overlay (x04_Check.py)'));
        items.push(new ActionItem('Validate Overlay', 'verified', 'aavikko.runValidate', 'action',
            'Sanity-check overlay placement (x05_Validate.py)'));

        return items;
    }
}