import * as fs from 'fs';
import { execGit } from './git';
import { log } from './logger';

/**
 * Writer for .conflict_decisions.yml.
 *
 * v0.1 rewrote the YAML with a hand-rolled line filter that corrupted the
 * file when entries had multi-line sub-fields or appeared in a different
 * order. v0.2 parses the simple structure into a model and re-serializes
 * the whole file deterministically.
 */

interface DecisionEntry {
    decision: string;
    decided_at?: string;
    upstream_commit?: string;
    extra?: Record<string, string>;
}

interface DecisionsDoc {
    header: string[];
    patches: Record<string, DecisionEntry>;
    mods: Record<string, DecisionEntry>;
}

function parseDecisions(content: string): DecisionsDoc {
    const doc: DecisionsDoc = { header: [], patches: {}, mods: {} };
    let section: 'patches' | 'mods' | null = null;
    let currentPath: string | null = null;

    for (const line of content.split('\n')) {
        const trimmed = line.trim();
        if (trimmed === 'patches:' || trimmed === 'mods:') {
            section = trimmed.slice(0, -1) as 'patches' | 'mods';
            currentPath = null;
            continue;
        }
        if (!section) {
            if (trimmed && !trimmed.startsWith('#')) {
                // Unknown top-level key — keep as header comment to be safe
                doc.header.push(`# ${trimmed}`);
            } else if (trimmed.startsWith('#')) {
                doc.header.push(line);
            }
            continue;
        }
        if (trimmed.startsWith('- ')) {
            currentPath = trimmed.substring(2).trim();
            doc[section][currentPath] = { decision: '' };
            continue;
        }
        if (currentPath && (line.startsWith('    ') || line.startsWith('\t'))) {
            const kv = trimmed.match(/^([a-z_]+):\s*(.*)$/);
            if (kv) {
                const entry = doc[section][currentPath];
                if (kv[1] === 'decision') {
                    entry.decision = kv[2];
                } else if (kv[1] === 'decided_at') {
                    entry.decided_at = kv[2];
                } else if (kv[1] === 'upstream_commit') {
                    entry.upstream_commit = kv[2];
                } else {
                    (entry.extra ??= {})[kv[1]] = kv[2];
                }
            }
        }
    }
    return doc;
}

function serializeDecisions(doc: DecisionsDoc): string {
    const out: string[] = [];
    const header = doc.header.length
        ? doc.header
        : ['# Aavikko conflict decisions', '# Managed by the Aavikko VS Code extension / x04_Check.py'];
    out.push(...header, '');

    const emitSection = (name: string, entries: Record<string, DecisionEntry>) => {
        out.push(`${name}:`);
        for (const [p, e] of Object.entries(entries)) {
            out.push(`  - ${p}`);
            out.push(`    decision: ${e.decision}`);
            if (e.decided_at) {
                out.push(`    decided_at: ${e.decided_at}`);
            }
            if (e.upstream_commit) {
                out.push(`    upstream_commit: ${e.upstream_commit}`);
            }
            for (const [k, v] of Object.entries(e.extra ?? {})) {
                out.push(`    ${k}: ${v}`);
            }
            out.push('');
        }
        out.push('');
    };

    emitSection('patches', doc.patches);
    emitSection('mods', doc.mods);
    return out.join('\n');
}

/** Record (or replace) a decision for a conflict. */
export async function writeDecision(
    decisionsPath: string,
    buildRoot: string,
    conflictType: 'patch' | 'mod',
    filePath: string,
    decision: string,
): Promise<void> {
    let content = '';
    try {
        content = fs.readFileSync(decisionsPath, 'utf-8');
    } catch { /* file doesn't exist yet */ }

    const doc = parseDecisions(content);
    const commit = (await execGit(buildRoot, ['rev-parse', 'HEAD'])).trim() || 'unknown';

    doc[conflictType === 'patch' ? 'patches' : 'mods'][filePath] = {
        decision,
        decided_at: new Date().toISOString(),
        upstream_commit: commit,
    };

    fs.writeFileSync(decisionsPath, serializeDecisions(doc), 'utf-8');
    log(`Decision written: ${conflictType} ${filePath} → ${decision}`);
}