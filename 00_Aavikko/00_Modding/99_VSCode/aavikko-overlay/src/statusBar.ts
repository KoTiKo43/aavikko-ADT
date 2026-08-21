import * as vscode from 'vscode';
import * as path from 'path';
import { StateManager, detectPathType } from './state';

/**
 * Status bar — persistent state indicator.
 *
 * v0.2: three visual states (applied / pristine / applied-with-issues) and
 * click opens a QuickPick action menu instead of blindly toggling.
 *
 * v0.2.2: added Pristine Upstream Warning banner (separate right-aligned item).
 *         Shows a pulsing red banner when overlay is NOT applied AND the active
 *         editor is an upstream file (Resources/, Content.*). Warns dev they're
 *         editing pristine upstream — any changes will be lost on Clear/git checkout.
 */
export class AavikkoStatusBar {
    private readonly item: vscode.StatusBarItem;
    private readonly pristineWarning: vscode.StatusBarItem;
    private pristineTimer: NodeJS.Timeout | null = null;
    private pristinePhase = 0;

    constructor(private readonly state: StateManager) {
        // ── Main status item (left side) ──
        this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
        this.item.command = 'aavikko.showActions';
        state.onDidChange(() => { this.update(); this.refreshPristineWarning(); });
        this.update();
        this.item.show();

        // ── Pristine upstream warning (right side, big red banner) ──
        // Only visible when: overlay NOT applied AND active editor is upstream file.
        // Uses error-themed colors so it stands out.
        this.pristineWarning = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Right, 200,
        );
        this.pristineWarning.command = 'aavikko.showActions';
        this.pristineWarning.text = '$(warning) EDITING CLEAN UPSTREAM — NO AAVIKKO CHANGES APPLIED $(warning)';
        this.pristineWarning.tooltip = new vscode.MarkdownString(
            '⚠ **WARNING** ⚠\n\n' +
            'Overlay is **NOT** applied. You are editing pristine upstream files.\n\n' +
            'Any changes you make here will be **LOST** when:\n' +
            '- You run `x02_Clear.py`\n' +
            '- You run `git checkout` / `git pull`\n' +
            '- VS Code reloads\n\n' +
            '### To save your changes properly:\n' +
            '1. Run `x00_Apply.py` first (status bar → "Aavikko: Pristine")\n' +
            '2. Edit the file (now it has overlay applied)\n' +
            '3. Run `x01_Generate.py` to save the diff as `.patch`\n' +
            '4. Or use the **Dirty Files** panel → **Generate Patch** button\n\n' +
            '**Click this banner to open the action menu.**'
        );
        this.pristineWarning.tooltip.isTrusted = true;
        this.pristineWarning.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
        this.pristineWarning.color = new vscode.ThemeColor('statusBarItem.errorForeground');

        // Update pristine warning when active editor changes
        vscode.window.onDidChangeActiveTextEditor(() => this.refreshPristineWarning());

        this.refreshPristineWarning();
    }

    private update(): void {
        const st = this.state.current;
        this.item.backgroundColor = undefined;
        if (st.state === 'applied') {
            const patches =
                (st.applied?.cs_patches_applied?.length ?? 0) +
                (st.applied?.robust_patches_applied?.length ?? 0);
            const failed = st.applied?.cs_patches_failed?.length ?? 0;
            // Only count files WITHOUT overlay as "dirty" — files WITH overlay
            // are expected to show as M/?? after Apply (they were copied there).
            // Counting them all would always show 500+ dirty right after Apply,
            // which is misleading.
            const dirty = st.dirty.filter(d => !d.has_overlay).length;
            const suffix = dirty > 0 ? ` · ${dirty} dirty` : '';
            this.item.text = `$(package) Aavikko: Applied (${patches}p${suffix})`;
            const lines = [
                `Overlay applied at ${st.applied?.at ?? '?'}`,
                `Patches: ${patches}` + (failed ? ` (${failed} FAILED)` : ''),
            ];
            if (dirty > 0) {
                lines.push(`${dirty} uncaptured change(s) — run Generate`);
            }
            const appliedTracked = st.dirty.filter(d => d.has_overlay).length;
            if (appliedTracked > 0) {
                lines.push(`${appliedTracked} overlay-tracked file(s) applied (normal)`);
            }
            lines.push('', 'Click for actions');
            this.item.tooltip = lines.join('\n');
            if (failed > 0) {
                this.item.backgroundColor =
                    new vscode.ThemeColor('statusBarItem.warningBackground');
            }
        } else {
            this.item.text = '$(circle-outline) Aavikko: Pristine';
            this.item.tooltip = 'Upstream is clean\nClick for actions';
        }
    }

    /**
     * Show / hide the pristine-upstream warning based on:
     *   1. Overlay must NOT be applied (state.state === 'pristine')
     *   2. Active editor must be an upstream file (Resources/, Content.*)
     *      NOT an Aavikko.* overlay file, NOT RobustToolbox submodule
     *      (RobustToolbox files are handled by the user separately — they
     *      rarely need pristine warnings because patches go through deploy_patch)
     */
    private refreshPristineWarning(): void {
        const st = this.state.current;

        // Overlay IS applied → hide warning
        if (st.state === 'applied') {
            this.pristineWarning.hide();
            this.stopPristineAnimation();
            return;
        }

        // Overlay NOT applied — check active editor
        const activeEditor = vscode.window.activeTextEditor;
        if (!activeEditor) {
            this.pristineWarning.hide();
            this.stopPristineAnimation();
            return;
        }

        const filePath = activeEditor.document.uri.fsPath;
        const buildRoot = this.state.buildRoot;
        if (!filePath.startsWith(buildRoot)) {
            this.pristineWarning.hide();
            this.stopPristineAnimation();
            return;
        }

        const rel = path.relative(buildRoot, filePath).replace(/\\/g, '/').replace(/^\.\//, '');
        // detectPathType returns 'resources' | 'robust' | 'content'
        // We warn for 'resources' and 'content' — both are upstream files.
        // 'robust' (RobustToolbox submodule) — patches go through deploy_patch, skip.
        const type = detectPathType(rel);
        if (type === 'robust') {
            this.pristineWarning.hide();
            this.stopPristineAnimation();
            return;
        }

        // Upstream file (Resources or Content) + overlay NOT applied → SHOW WARNING
        this.pristineWarning.show();
        this.startPristineAnimation();
    }

    /**
     * Start a slow "pulsing" animation by varying the warning text width.
     * VS Code status bar items don't support CSS animations, but we can
     * emulate one by changing `text` on a timer (every 700ms).
     *
     * The pulse cycles through 3 phases with different amounts of padding
     * around the warning icons — creates a slow "breathing" effect.
     */
    private startPristineAnimation(): void {
        if (this.pristineTimer) {
            return;  // already running
        }
        this.pristineTimer = setInterval(() => {
            this.pristinePhase = (this.pristinePhase + 1) % 3;
            const pad = this.pristinePhase === 0 ? '   '
                : this.pristinePhase === 1 ? '  '
                : ' ';
            this.pristineWarning.text =
                `${pad}$(warning) EDITING CLEAN UPSTREAM — NO AAVIKKO CHANGES APPLIED $(warning)${pad}`;
        }, 700);
    }

    private stopPristineAnimation(): void {
        if (this.pristineTimer) {
            clearInterval(this.pristineTimer);
            this.pristineTimer = null;
        }
    }

    async showActions(): Promise<void> {
        const st = this.state.current;
        const applied = st.state === 'applied';
        interface Action extends vscode.QuickPickItem {
            run: () => unknown;
        }
        const cmd = (id: string) => async () => { await vscode.commands.executeCommand(id); };
        const actions: Action[] = [
            applied
                ? { label: '$(discard) Clear Overlay', description: 'revert upstream to pristine', run: cmd('aavikko.clear') }
                : { label: '$(check) Apply Overlay', description: 'apply patches + copy mods', run: cmd('aavikko.apply') },
            { label: '$(diff) Generate All Patches', description: 'capture modified files into overlay', run: cmd('aavikko.generateAll') },
            { label: '$(shield) Check Conflicts', description: 'detect upstream conflicts', run: cmd('aavikko.runCheck') },
            { label: '$(verified) Validate Overlay', description: 'sanity-check placement', run: cmd('aavikko.runValidate') },
            { label: '$(refresh) Refresh Views', description: 're-read status', run: cmd('aavikko.refreshAll') },
            { label: '$(output) Show Log', description: 'Aavikko output channel', run: cmd('aavikko.showStatus') },
        ];
        const picked = await vscode.window.showQuickPick(actions, {
            title: `Aavikko — ${applied ? 'overlay applied' : 'upstream pristine'}`,
            placeHolder: 'Choose an action',
        });
        if (picked) {
            await picked.run();
        }
    }

    dispose(): void {
        this.stopPristineAnimation();
        this.item.dispose();
        this.pristineWarning.dispose();
    }
}