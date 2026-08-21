import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { initLogger, log, logError, showLog } from './logger';
import { getPython, invalidatePythonCache, runScript, runScriptInTerminal, disposeTerminal } from './python';
import { gitShow, applyPatchToContent } from './git';
import { StateManager, relNorm } from './state';
import { AavikkoDecorationProvider, PatchLineHighlighter } from './decorations';
import { AavikkoStatusBar } from './statusBar';
import { OverviewProvider } from './views/overview';
import { DirtyFilesProvider, DirtyFileItem } from './views/dirtyFiles';
import { ConflictTreeProvider, extractConflictData } from './views/conflicts';
import { writeDecision } from './decisions';

let state: StateManager;
let decorations: AavikkoDecorationProvider;
let highlighter: PatchLineHighlighter;
let statusBar: AavikkoStatusBar;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
    initLogger();
    const folders = vscode.workspace.workspaceFolders;
    if (!folders) {
        return;
    }
    const buildRoot = findBuildRoot(folders[0].uri.fsPath);
    if (!buildRoot) {
        log('00_Aavikko/00_Modding/00_Patcher/x00_Apply.py not found — extension inactive');
        return;
    }
    const patcherDir = path.join(buildRoot, '00_Aavikko/00_Modding', '00_Patcher');
    log(`Build root: ${buildRoot}`);
    vscode.commands.executeCommand('setContext', 'aavikko.isActive', true);

    state = new StateManager(buildRoot, patcherDir);
    decorations = new AavikkoDecorationProvider(buildRoot, state);
    highlighter = new PatchLineHighlighter(buildRoot);
    statusBar = new AavikkoStatusBar(state);

    context.subscriptions.push(
        vscode.window.registerFileDecorationProvider(decorations),
        { dispose: () => highlighter.dispose() },
        { dispose: () => statusBar.dispose() },
        { dispose: () => state.dispose() },
    );
    state.onDidChange(() => {
        decorations.rebuild();
        highlighter.invalidate();
    });

    // ── Tree views ──
    const overviewProvider = new OverviewProvider(state);
    const dirtyProvider = new DirtyFilesProvider(buildRoot, state);
    const conflictProvider = new ConflictTreeProvider(state);
    context.subscriptions.push(
        vscode.window.registerTreeDataProvider('aavikkoOverview', overviewProvider),
        vscode.window.registerTreeDataProvider('aavikkoDirtyFiles', dirtyProvider),
        vscode.window.registerTreeDataProvider('aavikkoConflicts', conflictProvider),
    );

    registerCommands(context, buildRoot, patcherDir, {
        overviewProvider, dirtyProvider, conflictProvider,
    });

    // ── Watchers ──
    const refresh = () => { void state.refresh(); };

    const appliedWatcher = vscode.workspace.createFileSystemWatcher(
        '**/00_Aavikko/00_Modding/00_Patcher/.applied');
    appliedWatcher.onDidCreate(refresh);
    appliedWatcher.onDidDelete(refresh);
    appliedWatcher.onDidChange(refresh);

    const overlayWatcher = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(vscode.Uri.file(buildRoot), '{' + [
            '00_Aavikko/01_Resources/Mods/**', '00_Aavikko/01_Resources/Patches/**',
            '00_Aavikko/02_Content/Mods/**', '00_Aavikko/02_Content/Patches/**',
            '00_Aavikko/03_RobustToolbox/Mods/**', '00_Aavikko/03_RobustToolbox/Patches/**',
            '00_Aavikko/00_Modding/00_Patcher/.upstream_state.json',
            '00_Aavikko/00_Modding/00_Patcher/.conflict_decisions.yml',
        ].join(',') + '}'));

    let debounce: NodeJS.Timeout | null = null;
    const debouncedRefresh = () => {
        if (debounce) { clearTimeout(debounce); }
        debounce = setTimeout(refresh, 500);
    };
    overlayWatcher.onDidChange(debouncedRefresh);
    overlayWatcher.onDidCreate(debouncedRefresh);
    overlayWatcher.onDidDelete(debouncedRefresh);

    context.subscriptions.push(appliedWatcher, overlayWatcher);

    // Refresh dirty list on save (setting-controlled)
    context.subscriptions.push(vscode.workspace.onDidSaveTextDocument(() => {
        if (vscode.workspace.getConfiguration('aavikko').get('autoRefreshOnSave', true)) {
            refresh();
        }
    }));

    // Settings changes
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(e => {
        if (e.affectsConfiguration('aavikko.pythonPath')) {
            invalidatePythonCache();
        }
        if (e.affectsConfiguration('aavikko.highlightPatchedLines')) {
            highlighter.invalidate();
        }
    }));

    // Highlight patched lines in visible editors
    context.subscriptions.push(
        vscode.window.onDidChangeActiveTextEditor(e => { if (e) { highlighter.update(e); } }),
        vscode.window.onDidChangeVisibleTextEditors(es => es.forEach(e => highlighter.update(e))),
    );

    // Initial load
    await state.refresh();
    for (const editor of vscode.window.visibleTextEditors) {
        highlighter.update(editor);
    }
    log('Extension activated');
}

export function deactivate(): void {
    disposeTerminal();
}

// ── Commands ────────────────────────────────────────────────────────────────

interface Providers {
    overviewProvider: OverviewProvider;
    dirtyProvider: DirtyFilesProvider;
    conflictProvider: ConflictTreeProvider;
}

function registerCommands(
    context: vscode.ExtensionContext,
    buildRoot: string,
    patcherDir: string,
    providers: Providers,
): void {
    const refresh = () => state.refresh();

    /** Run a script with progress + captured output; refresh state after. */
    async function runHeadless(script: string, args: string[], title: string): Promise<boolean> {
        // Mark state as "busy" — pauses polling while Apply/Clear/Generate runs
        // (these scripts take 30s-2min; polling during them would thrash Python)
        const doneBusy = state.withBusy();
        try {
            const result = await vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification, title: `Aavikko: ${title}…`, cancellable: false },
                () => runScript(patcherDir, script, args, 300_000),
            );
            if (!result) {
                return false;
            }
            if (result.stdout.trim()) { log(result.stdout.trim()); }
            if (result.stderr.trim()) { log(`stderr: ${result.stderr.trim()}`); }
            await refresh();
            if (result.code !== 0) {
                const choice = await vscode.window.showErrorMessage(
                    `Aavikko: ${title} failed (exit ${result.code})`, 'Show Log');
                if (choice === 'Show Log') { showLog(); }
                return false;
            }
            vscode.window.showInformationMessage(`Aavikko: ${title} — done`);
            return true;
        } finally {
            doneBusy();
        }
    }

    const cmd = (id: string, fn: (...args: unknown[]) => unknown) =>
        context.subscriptions.push(vscode.commands.registerCommand(id, fn));

    // ── Lifecycle ──
    cmd('aavikko.apply', () => runHeadless('x00_Apply.py', [], 'Applying overlay'));
    cmd('aavikko.clear', () => runHeadless('x02_Clear.py', [], 'Clearing overlay'));
    cmd('aavikko.toggleOverlay', async () => {
        await refresh();
        await runHeadless(state.applied ? 'x02_Clear.py' : 'x00_Apply.py', [],
            state.applied ? 'Clearing overlay' : 'Applying overlay');
    });
    cmd('aavikko.generateAll', () => runHeadless('x01_Generate.py', ['--all'], 'Generating patches'));
    cmd('aavikko.runValidate', () => runHeadless('x05_Validate.py', [], 'Validating overlay'));
    cmd('aavikko.refreshAll', async () => { await refresh(); });

    // x04_Check.py is interactive (conflict prompts) — run in terminal
    cmd('aavikko.runCheck', async () => {
        const python = await getPython();
        if (!python) {
            vscode.window.showErrorMessage('Aavikko: Python not found.');
            return;
        }
        // -X utf8 forces UTF-8 mode (Windows cp1251 fix)
        runScriptInTerminal(patcherDir, `${python} -X utf8 x04_Check.py`);
    });

    cmd('aavikko.showStatus', async () => {
        const python = await getPython();
        if (python) {
            runScriptInTerminal(patcherDir, `${python} -X utf8 x03_Status.py`);
        }
        showLog();
    });

    cmd('aavikko.showActions', () => statusBar.showActions());

    // ── Dirty files ──
    cmd('aavikko.openDirtyFile', async (filePath: unknown) => {
        if (typeof filePath !== 'string') { return; }
        const doc = await vscode.workspace.openTextDocument(filePath);
        await vscode.window.showTextDocument(doc);
    });

    cmd('aavikko.revealDirtyFile', async (item: unknown) => {
        const filePath = item instanceof DirtyFileItem ? item.filePath : null;
        if (filePath) {
            await vscode.commands.executeCommand('revealInExplorer', vscode.Uri.file(filePath));
        }
    });

    const generateFor = async (item: unknown, restore: boolean) => {
        let filePath: string | undefined;
        if (item instanceof DirtyFileItem) {
            filePath = item.filePath;
        } else if (item && typeof item === 'object' && 'filePath' in item) {
            filePath = String((item as { filePath: unknown }).filePath);
        }
        if (!filePath) {
            filePath = vscode.window.activeTextEditor?.document.uri.fsPath;
        }
        if (!filePath || !fs.existsSync(filePath)) {
            vscode.window.showErrorMessage(
                'Aavikko: No file selected. Open a file or use the Dirty Files panel.');
            return;
        }
        const rel = relNorm(buildRoot, filePath);
        const args = restore ? [rel, '--restore'] : [rel];
        await runHeadless('x01_Generate.py', args, `Capturing ${path.basename(rel)}`);
    };
    cmd('aavikko.generatePatch', (item: unknown) => generateFor(item, false));
    cmd('aavikko.generatePatchRestore', (item: unknown) => generateFor(item, true));

    cmd('aavikko.refreshDirtyFiles', () => refresh());
    cmd('aavikko.refreshConflicts', () => refresh());

    // ── Virtual diff documents ──
    registerDiffProviders(context, buildRoot);

    // ── Conflict diff commands ──
    const closeConflictTabsFor = (filePath: string) => {
        const fileName = path.basename(filePath);
        for (const group of vscode.window.tabGroups.all) {
            for (const tab of group.tabs) {
                if (tab.label.includes(fileName) && tab.label.includes('—')) {
                    void vscode.window.tabGroups.close(tab);
                }
            }
        }
    };

    cmd('aavikko.openConflictDiff', async (arg: unknown) => {
        const data = extractConflictData(arg);
        if (!data) { return; }
        closeConflictTabsFor(data.path);
        await vscode.commands.executeCommand('vscode.diff',
            vscode.Uri.parse(`aavikko-old-upstream:///${data.path}`),
            vscode.Uri.file(path.join(buildRoot, data.path)),
            `${path.basename(data.path)} — upstream diff (old → new)`,
            { preview: true });
    });

    cmd('aavikko.openOurVersion', async (arg: unknown) => {
        const data = extractConflictData(arg);
        if (!data) { return; }
        closeConflictTabsFor(data.path);
        await vscode.commands.executeCommand('vscode.diff',
            vscode.Uri.parse(`aavikko-our-version:///${data.path}`),
            vscode.Uri.file(path.join(buildRoot, data.path)),
            `${path.basename(data.path)} — our version vs new upstream`,
            { preview: true });
    });

    cmd('aavikko.openBothDiffs', async (arg: unknown) => {
        const data = extractConflictData(arg);
        if (!data) { return; }
        closeConflictTabsFor(data.path);
        const fileName = path.basename(data.path);
        await vscode.commands.executeCommand('vscode.diff',
            vscode.Uri.parse(`aavikko-old-upstream:///${data.path}`),
            vscode.Uri.file(path.join(buildRoot, data.path)),
            `${fileName} — upstream diff (old → new)`,
            { preview: false, viewColumn: vscode.ViewColumn.One });
        await vscode.commands.executeCommand('vscode.diff',
            vscode.Uri.parse(`aavikko-our-version:///${data.path}`),
            vscode.Uri.file(path.join(buildRoot, data.path)),
            `${fileName} — our version vs new upstream`,
            { preview: false, viewColumn: vscode.ViewColumn.Two });
    });

    cmd('aavikko.openForEditing', async (arg: unknown) => {
        const data = extractConflictData(arg);
        if (!data) { return; }
        closeConflictTabsFor(data.path);
        const full = path.join(buildRoot, data.path);
        if (fs.existsSync(full)) {
            const doc = await vscode.workspace.openTextDocument(full);
            await vscode.window.showTextDocument(doc, { preview: false });
        }
    });

    // ── Conflict resolution (double-click-to-confirm) ──
    const pending = new Map<string, NodeJS.Timeout>();

    async function resolveWithConfirm(
        key: string, label: string, desc: string, action: () => Promise<void>,
    ): Promise<void> {
        if (pending.has(key)) {
            clearTimeout(pending.get(key)!);
            pending.delete(key);
            await action();
            await refresh();
            vscode.window.showInformationMessage(`Aavikko: ${label}`);
            return;
        }
        pending.set(key, setTimeout(() => pending.delete(key), 5000));
        vscode.window.showInformationMessage(`Click again to confirm: ${label} — ${desc}`);
    }

    const patchDecisions: Record<string, [string, string, string]> = {
        fr: ['Force Replace', 'fr', 'use our patch, forget upstream change'],
        u: ['Updated', 'u', 'patch was updated manually (regenerate)'],
        s: ['Skip Patch', 's', 'do not apply this patch at all'],
        i: ['Ignore', 'i', 'skip for now — will reappear on next Check'],
    };
    for (const [key, [label, decision, desc]] of Object.entries(patchDecisions)) {
        cmd(`aavikko.resolvePatch.${key}`, async (item: unknown) => {
            const data = extractConflictData(item);
            if (!data) { return; }
            await resolveWithConfirm(`patch:${data.path}:${key}`, `${label}: ${data.path}`, desc, async () => {
                await writeDecision(state.decisionsFilePath, buildRoot, 'patch', data.path, decision);
            });
        });
    }

    const modDecisions: Record<string, [string, string, string]> = {
        k: ['Keep Our Mod', 'k', 'override upstream with our version'],
        r: ['Remove Our Mod', 'r', 'use upstream version, delete our mod'],
        g: ['Convert to Patch', 'g', 'move Mods/ → Patches/ (file now exists upstream)'],
        i: ['Ignore', 'i', 'skip for now — will reappear on next Check'],
    };
    for (const [key, [label, decision, desc]] of Object.entries(modDecisions)) {
        cmd(`aavikko.resolveMod.${key}`, async (item: unknown) => {
            const data = extractConflictData(item);
            if (!data) { return; }
            await resolveWithConfirm(`mod:${data.path}:${key}`, `${label}: ${data.path}`, desc, async () => {
                if (decision === 'g') {
                    convertModToPatch(buildRoot, data.path);
                } else {
                    await writeDecision(state.decisionsFilePath, buildRoot, 'mod', data.path, decision);
                }
            });
        });
    }
}

// ── Virtual diff document providers ─────────────────────────────────────────

function registerDiffProviders(context: vscode.ExtensionContext, buildRoot: string): void {
    // Old upstream content (git show HEAD:path)
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(
        'aavikko-old-upstream', new (class implements vscode.TextDocumentContentProvider {
            async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
                const filePath = uri.path.replace(/^\//, '');
                const content = await gitShow(buildRoot, 'HEAD', filePath);
                if (content !== null) {
                    return content;
                }
                return `// This file did not exist in old upstream (HEAD).\n` +
                    `// It was added by upstream in the latest update.\n// Path: ${filePath}`;
            }
        })(),
    ));

    // Our overlay version (patch applied to old upstream, or direct copy)
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(
        'aavikko-our-version', new (class implements vscode.TextDocumentContentProvider {
            async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
                const filePath = uri.path.replace(/^\//, '');
                const overlayPath = findOverlayFile(buildRoot, filePath);
                if (!overlayPath) {
                    return `// No overlay file found for ${filePath}`;
                }
                if (overlayPath.endsWith('.patch')) {
                    const oldContent = await gitShow(buildRoot, 'HEAD', filePath);
                    if (oldContent === null) {
                        return '// Error: could not get old upstream version to apply patch to';
                    }
                    // v0.2: git apply in a temp dir (Windows-safe, no unix `patch`)
                    const patched = await applyPatchToContent(
                        oldContent, overlayPath, path.basename(filePath));
                    if (patched !== null) {
                        return patched;
                    }
                    try {
                        const raw = fs.readFileSync(overlayPath, 'utf-8');
                        return '// Patch could not be applied to old upstream (context changed).\n' +
                            `// Showing raw patch content:\n\n${raw}`;
                    } catch {
                        return `// Error reading patch: ${overlayPath}`;
                    }
                }
                try {
                    return fs.readFileSync(overlayPath, 'utf-8');
                } catch {
                    return `// Error reading overlay file: ${overlayPath}`;
                }
            }
        })(),
    ));
}

/** Locate the overlay file (patch or mod copy) backing an upstream path. */
function findOverlayFile(buildRoot: string, rel: string): string | null {
    const first = (...candidates: string[]): string | null =>
        candidates.find(c => fs.existsSync(c)) ?? null;

    if (rel.startsWith('Resources/')) {
        const mirror = rel.substring('Resources/'.length);
        return first(
            path.join(buildRoot, '00_Aavikko/01_Resources', 'Patches', mirror),
            path.join(buildRoot, '00_Aavikko/01_Resources', 'Mods', mirror));
    }
    if (rel.startsWith('RobustToolbox/')) {
        const inner = rel.substring('RobustToolbox/'.length);
        return first(
            path.join(buildRoot, '00_Aavikko/03_RobustToolbox', 'Patches', `${inner}.patch`),
            path.join(buildRoot, '00_Aavikko/03_RobustToolbox', 'Mods', inner));
    }
    return first(
        path.join(buildRoot, '00_Aavikko/02_Content', 'Patches', `${rel}.patch`),
        path.join(buildRoot, '00_Aavikko/02_Content', 'Mods', rel));
}

/** Move a mod file from Mods/ to Patches/ (mod → patch conversion). */
function convertModToPatch(buildRoot: string, filePath: string): void {
    let src: string;
    let dst: string;
    if (filePath.startsWith('Resources/')) {
        const rel = filePath.substring('Resources/'.length);
        src = path.join(buildRoot, '00_Aavikko/01_Resources', 'Mods', rel);
        dst = path.join(buildRoot, '00_Aavikko/01_Resources', 'Patches', rel);
    } else {
        src = path.join(buildRoot, '00_Aavikko/02_Content', 'Mods', filePath);
        dst = path.join(buildRoot, '00_Aavikko/02_Content', 'Patches', filePath);
    }
    if (!fs.existsSync(src)) {
        vscode.window.showErrorMessage(`Aavikko: Cannot convert — source not found: ${src}`);
        return;
    }
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    try {
        fs.renameSync(src, dst);
        vscode.window.showInformationMessage(
            `Aavikko: Moved ${path.basename(src)} from Mods/ to Patches/`);
    } catch (e) {
        try {
            fs.copyFileSync(src, dst);
            fs.unlinkSync(src);
            vscode.window.showInformationMessage(
                `Aavikko: Copied ${path.basename(src)} from Mods/ to Patches/`);
        } catch (e2) {
            vscode.window.showErrorMessage(`Aavikko: Failed to move file: ${(e2 as Error).message}`);
            logError('convertModToPatch failed', e);
        }
    }
}

// ── Build root discovery ────────────────────────────────────────────────────

function findBuildRoot(workspacePath: string): string | null {
    for (const candidate of [workspacePath, path.join(workspacePath, '..'), path.join(workspacePath, '..', '..')]) {
        if (fs.existsSync(path.join(candidate, '00_Aavikko/00_Modding', '00_Patcher', 'x00_Apply.py'))) {
            return candidate;
        }
    }
    return searchForMarker(workspacePath, 3);
}

function searchForMarker(dir: string, maxDepth: number): string | null {
    if (maxDepth <= 0) { return null; }
    if (fs.existsSync(path.join(dir, '00_Aavikko/00_Modding', '00_Patcher', 'x00_Apply.py'))) {
        return dir;
    }
    let entries: fs.Dirent[];
    try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
        return null;
    }
    for (const entry of entries) {
        if (entry.isDirectory() && !entry.name.startsWith('.') &&
            !['node_modules', 'bin', 'obj', 'Resources'].includes(entry.name)) {
            const found = searchForMarker(path.join(dir, entry.name), maxDepth - 1);
            if (found) { return found; }
        }
    }
    return null;
}