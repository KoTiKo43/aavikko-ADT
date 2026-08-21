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
            // Count ALL applied files (Resources copies + Content/Robust .cs.patch).
            // The old logic only counted cs_patches_applied which is 0 in ADT-overlay
            // (where everything is Resources copies).
            const csPatches = st.applied?.cs_patches_applied?.length ?? 0;
            const robustPatches = st.applied?.robust_patches_applied?.length ?? 0;
            const patchesCopied = st.applied?.counts?.patches_copied ?? 0;
            const modsCopied = st.applied?.counts?.mods_copied ?? 0;
            const contentMods = st.applied?.counts?.content_mods_copied ?? 0;
            const robustMods = st.applied?.counts?.robust_mods_copied ?? 0;
            const totalPatches = csPatches + robustPatches + patchesCopied;
            const totalMods = modsCopied + contentMods + robustMods;
            const stateItem = new InfoItem('Overlay applied', `${totalPatches} patches · ${totalMods} mods`, 'check-all',
                `Applied at ${st.applied?.at ?? '?'}\nHEAD: ${st.applied?.head_commit ?? '?'}\n` +
                `Patches: ${csPatches} cs + ${robustPatches} robust + ${patchesCopied} resources\n` +
                `Mods: ${modsCopied} resources + ${contentMods} content + ${robustMods} robust`);
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
        // Split dirty list into two groups:
        //   - applied files (has_overlay=true) — these are files in upstream
        //     that Apply.py overwrote with the overlay version. They show as
        //     'M' or '??' in git status because the working tree differs from
        //     HEAD. They are NOT uncaptured — they're tracked and applied.
        //   - truly uncaptured (has_overlay=false) — files the user edited
        //     without an overlay file yet. These need Generate to capture.
        const appliedDirty = st.dirty.filter(d => d.has_overlay);
        const uncapturedDirty = st.dirty.filter(d => !d.has_overlay);
        if (appliedDirty.length > 0) {
            items.push(new InfoItem('Applied (tracked)', `${appliedDirty.length}`, 'package',
                `Overlay-tracked files currently applied to upstream.\n` +
                `These show as modified in git because Apply.py overwrote them.\n` +
                `This is normal — run Clear to revert to pristine upstream.`));
        }
        if (uncapturedDirty.length > 0) {
            items.push(new InfoItem('Uncaptured changes', `${uncapturedDirty.length}`, 'edit',
                'Modified upstream files with NO overlay file yet.\n' +
                'Run Generate to capture them into the overlay.'));
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