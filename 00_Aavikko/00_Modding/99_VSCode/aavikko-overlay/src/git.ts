import * as cp from 'child_process';

/** Git helpers — all via execFile (no shell → no quoting/injection issues). */

export function execGit(cwd: string, args: string[], maxBuffer = 32 * 1024 * 1024): Promise<string> {
    return new Promise((resolve) => {
        cp.execFile('git', args, { cwd, encoding: 'utf-8', maxBuffer }, (err, stdout) => {
            resolve(err ? '' : (stdout ?? ''));
        });
    });
}

/** `git show <ref>:<path>` — file content at a revision. Returns null if absent. */
export function gitShow(cwd: string, ref: string, filePath: string): Promise<string | null> {
    return new Promise((resolve) => {
        cp.execFile(
            'git',
            ['show', `${ref}:${filePath}`],
            { cwd, encoding: 'utf-8', maxBuffer: 32 * 1024 * 1024 },
            (err, stdout) => resolve(err ? null : stdout),
        );
    });
}

/**
 * Apply a unified-diff patch to a base content string, entirely in-memory:
 * writes both to a temp dir and runs `git apply` there.
 * Returns patched content, or null if the patch doesn't apply.
 *
 * Replaces v0.1's unix-only `patch -s -o` call (breaks on Windows).
 */
export async function applyPatchToContent(
    baseContent: string,
    patchPath: string,
    fileName: string,
): Promise<string | null> {
    const os = await import('os');
    const fs = await import('fs');
    const path = await import('path');
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'aavikko-preview-'));
    try {
        const target = path.join(tmpDir, fileName);
        fs.writeFileSync(target, baseContent, 'utf-8');
        const ok = await new Promise<boolean>((resolve) => {
            cp.execFile(
                'git',
                ['apply', '--whitespace=nowarn', patchPath],
                { cwd: tmpDir, encoding: 'utf-8' },
                (err) => resolve(!err),
            );
        });
        if (!ok) {
            return null;
        }
        return fs.readFileSync(target, 'utf-8');
    } catch {
        return null;
    } finally {
        try {
            fs.rmSync(tmpDir, { recursive: true, force: true });
        } catch { /* best-effort cleanup */ }
    }
}