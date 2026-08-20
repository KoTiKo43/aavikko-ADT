import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { StateManager } from './state';

/**
 * File explorer badges + inline editor highlight of patched lines.
 *
 * v0.2: decorations are computed from the Status.py overlay inventory
 * (single source of truth) instead of a parallel JS re-scan.
 */

type OverlayMark = 'mod' | 'patch';

export class AavikkoDecorationProvider implements vscode.FileDecorationProvider {
    private readonly _onDidChange = new vscode.EventEmitter<vscode.Uri | vscode.Uri[]>();
    readonly onDidChangeFileDecorations = this._onDidChange.event;

    private fileTypes = new Map<string, Set<OverlayMark>>();
    private dirTypes = new Map<string, Set<OverlayMark>>();

    constructor(
        private readonly buildRoot: string,
        private readonly state: StateManager,
    ) {}

    provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
        const fTypes = this.fileTypes.get(uri.fsPath);
        if (fTypes) {
            return this.makeDecoration(fTypes);
        }
        const dTypes = this.dirTypes.get(uri.fsPath);
        if (dTypes) {
            return this.makeDecoration(dTypes);
        }
        return undefined;
    }

    private makeDecoration(types: Set<OverlayMark>): vscode.FileDecoration | undefined {
        const hasMod = types.has('mod');
        const hasPatch = types.has('patch');
        // One badge slot is taken by git — differentiate via color.
        if (hasMod && hasPatch) {
            return new vscode.FileDecoration('MP', 'Aavikko: mods + patches',
                new vscode.ThemeColor('aavikko.bothColor'));
        }
        if (hasMod) {
            return new vscode.FileDecoration('Md', 'Aavikko: mod (new file)',
                new vscode.ThemeColor('aavikko.modColor'));
        }
        if (hasPatch) {
            return new vscode.FileDecoration('Pc', 'Aavikko: patch (modified upstream)',
                new vscode.ThemeColor('aavikko.patchColor'));
        }
        return undefined;
    }

    /** Rebuild the decoration map from the current overlay inventory. */
    rebuild(): void {
        const map = new Map<string, Set<OverlayMark>>();
        const st = this.state.current;
        if (!this.state.applied) {
            this.fileTypes = map;
            this.dirTypes = new Map();
            this._onDidChange.fire([]);
            return;
        }
        const at = (...segs: string[]) => path.join(this.buildRoot, ...segs);
        const add = (abs: string, t: OverlayMark) => {
            let s = map.get(abs);
            if (!s) {
                s = new Set();
                map.set(abs, s);
            }
            s.add(t);
        };

        // Resources overlay → badge on the upstream Resources/ file
        for (const rel of st.overlay.resource_mods) {
            add(at('Resources', rel), 'mod');
        }
        for (const rel of st.overlay.resource_patches) {
            add(at('Resources', rel), 'patch');
        }
        // Content/RT mods → badge on the overlay file itself (new files)
        for (const rel of st.overlay.content_mods) {
            add(at('00_Aavikko/02_Content', 'Mods', rel), 'mod');
        }
        for (const rel of st.overlay.robust_mods) {
            add(at('00_Aavikko/03_RobustToolbox', 'Mods', rel), 'mod');
        }
        // Content/RT patches → badge on the upstream file
        for (const rel of st.overlay.content_patches) {
            const up = rel.replace(/\.patch$/, '');
            add(at(up), 'patch');
        }
        for (const rel of st.overlay.robust_patches) {
            const up = rel.replace(/\.patch$/, '');
            add(at('RobustToolbox', up), 'patch');
        }

        this.fileTypes = map;
        this.dirTypes = this.computeDirTypes(map);
        this._onDidChange.fire([]);
    }

    private computeDirTypes(fileTypes: Map<string, Set<OverlayMark>>): Map<string, Set<OverlayMark>> {
        const dirTypes = new Map<string, Set<OverlayMark>>();
        for (const [filePath, types] of fileTypes) {
            let dir = path.dirname(filePath);
            const root = path.parse(filePath).root;
            for (let i = 0; dir && dir !== root && i < 20; i++) {
                let existing = dirTypes.get(dir);
                if (!existing) {
                    existing = new Set();
                    dirTypes.set(dir, existing);
                }
                for (const t of types) {
                    existing.add(t);
                }
                dir = path.dirname(dir);
            }
        }
        return dirTypes;
    }
}

// ── Inline patch highlighting ───────────────────────────────────────────────

/** Parses added-line numbers (1-indexed, new-file side) from a unified diff. */
export function parsePatchAddedLines(patchContent: string): number[] {
    const added: number[] = [];
    let newLineNum = 0;
    let inHunk = false;
    for (const line of patchContent.split('\n')) {
        const hunk = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (hunk) {
            newLineNum = parseInt(hunk[1], 10);
            inHunk = true;
            continue;
        }
        if (!inHunk) { continue; }
        if (line.startsWith('diff --git') || line.startsWith('---') || line.startsWith('+++')) {
            inHunk = false;
            continue;
        }
        if (line.startsWith('+')) {
            added.push(newLineNum);
            newLineNum++;
        } else if (line.startsWith(' ') || line === '') {
            newLineNum++;
        }
        // '-': removed lines don't exist on the new side; '\': no-newline marker
    }
    return added;
}

export class PatchLineHighlighter {
    private readonly decoration = vscode.window.createTextEditorDecorationType({
        backgroundColor: 'rgba(255, 152, 0, 0.15)',
        isWholeLine: true,
        overviewRulerColor: 'rgba(255, 152, 0, 0.7)',
        overviewRulerLane: vscode.OverviewRulerLane.Right,
    });
    private cache = new Map<string, number[]>();

    constructor(private readonly buildRoot: string) {}

    private get enabled(): boolean {
        return vscode.workspace.getConfiguration('aavikko').get('highlightPatchedLines', true);
    }

    invalidate(): void {
        this.cache.clear();
        for (const editor of vscode.window.visibleTextEditors) {
            this.update(editor);
        }
    }

    update(editor: vscode.TextEditor): void {
        if (!this.enabled) {
            editor.setDecorations(this.decoration, []);
            return;
        }
        const filePath = editor.document.uri.fsPath;
        let added = this.cache.get(filePath);
        if (added === undefined) {
            added = this.loadAddedLines(filePath);
            this.cache.set(filePath, added);
        }
        const ranges: vscode.Range[] = [];
        for (const lineNum of added) {
            const idx = lineNum - 1;
            if (idx >= 0 && idx < editor.document.lineCount) {
                ranges.push(editor.document.lineAt(idx).range);
            }
        }
        editor.setDecorations(this.decoration, ranges);
    }

    private loadAddedLines(filePath: string): number[] {
        const patchPath = this.findPatchForFile(filePath);
        if (!patchPath) {
            return [];
        }
        try {
            return parsePatchAddedLines(fs.readFileSync(patchPath, 'utf-8'));
        } catch {
            return [];
        }
    }

    private findPatchForFile(filePath: string): string | null {
        const rel = path.relative(this.buildRoot, filePath).replace(/\\/g, '/');
        if (rel.startsWith('..')) {
            return null;
        }
        if (rel.startsWith('RobustToolbox/')) {
            const inner = rel.substring('RobustToolbox/'.length);
            const candidate = path.join(this.buildRoot, '00_Aavikko/03_RobustToolbox', 'Patches', `${inner}.patch`);
            return fs.existsSync(candidate) ? candidate : null;
        }
        const candidate = path.join(this.buildRoot, '00_Aavikko/02_Content', 'Patches', `${rel}.patch`);
        return fs.existsSync(candidate) ? candidate : null;
    }

    dispose(): void {
        this.decoration.dispose();
    }
}