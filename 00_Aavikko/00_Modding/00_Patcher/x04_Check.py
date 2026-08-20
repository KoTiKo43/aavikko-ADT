#!/usr/bin/env python3
"""
Check.py — detect upstream changes that conflict with Aavikko overlay.

Two conflict types:

A. Patches/ conflict — upstream file changed since last Check, we have override:
   - Resources/Patches/<path>  → upstream Resources/<path> changed
   - Content/Patches/<path>.cs.patch → upstream Content.<path> changed
   Creates temp snapshots for developer to review and update patch.

B. Mods/ conflict — upstream added file at same path as our Mods/ file:
   - Resources/Mods/<path>  → upstream now has Resources/<path>
   - Content/Mods/<path>    → upstream now has Content.<path>

State tracking:
  00_Aavikko/00_Modding/.upstream_state.json — records upstream commit + sha256 of
  each upstream file that corresponds to our Patches/ and Mods/ entries.
  First run: records baseline (no conflicts).
  Subsequent runs: compares current upstream sha256 with recorded.

Decisions:
  00_Aavikko/00_Modding/.conflict_decisions.yml — developer's decisions persist.
  Apply.py reads this file and refuses to run if unresolved conflicts exist.

Usage:
  python3 x04_Check.py                    # interactive — ask developer for each conflict
  python3 x04_x04_Check.py --non-interactive  # CI mode — default 'ignore' for all, just report
  python3 x04_x04_Check.py --baseline         # force re-record baseline (forget all conflicts)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Force UTF-8 for stdout/stderr — Windows default is cp1251 which can't encode
# Unicode characters like →, —, ✓ used in print() statements.
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        pass

# Shared utilities — keeps run/atomic_write logic in one place.
# We import lazily (only the helpers we use) to avoid pulling in
# RESOURCES_DIR / BUILD_ROOT constants that already exist locally above.
try:
    from l00_common import atomic_write_text as _atomic_write_text_impl
except ImportError:
    _atomic_write_text_impl = None  # fall back to local copy below

# Validate git commit hashes (hex, 7-40 chars)
COMMIT_RE = re.compile(r'^[0-9a-f]{7,40}$')

from l01_ui import (
    header, section, divider, kv, ok, info, warn, error, fatal,
    skip, hint, tag, bullet, progress_iter, summary_table,
    success_banner, fail_banner, dim, bold,
    green, yellow, red, cyan, magenta,
)

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_ROOT = SCRIPT_DIR.parent.parent.parent
RESOURCES_DIR = BUILD_ROOT / "00_Aavikko/01_Resources"
CONTENT_DIR = BUILD_ROOT / "00_Aavikko/02_Content"
STATE_FILE = SCRIPT_DIR / ".upstream_state.json"
DECISIONS_FILE = SCRIPT_DIR / ".conflict_decisions.yml"


def run(cmd, cwd: Path | None = None) -> tuple[str, str, int]:
    """Run a command. Accepts either a string (shell-style) OR an argv list.

    Cross-platform: never uses shell=True. If `cmd` is a string, it's split
    via shlex.split (which can BREAK on Windows paths with backslashes if
    they're not properly quoted). For safety, prefer passing an argv list:

        run(["git", "status", "--porcelain"])  # SAFE
        run("git rev-parse HEAD")               # OK (no paths)
    """
    if isinstance(cmd, str):
        try:
            argv = shlex.split(cmd)
        except ValueError:
            result = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", errors="replace"
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        if not argv:
            return "", "", 0
    else:
        argv = list(cmd)
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def sha256_file(path: Path) -> str | None:
    """Calculate SHA256 of a file on disk (working tree). Returns None if missing.

    NOTE: This reads from WORKING TREE. After Apply.py the working tree contains
    our overlay files, so this is NOT the upstream version. For checking upstream
    state, use sha256_git_head() instead — reads from `git show HEAD:<path>`.
    """
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_git_head(rel_path: str) -> str | None:
    """Calculate SHA256 of a file as it exists in git HEAD (committed upstream).

    Uses `git show HEAD:<path>` to read the blob content (NOT working tree).
    This is critical: after Apply.py the working tree contains our overlay
    files, so reading working tree would report ALL files as "changed"
    vs baseline. We must compare against what's actually committed upstream.

    Args:
      rel_path: path relative to BUILD_ROOT (e.g. "Resources/Audio/foo.ogg",
                "Content.Shared/Botany/Systems/PlantSystem.cs")

    Returns:
      SHA256 hex of the file content in HEAD, or None if not tracked in HEAD.
    """
    # git show HEAD:<path> — outputs blob content to stdout (binary)
    # Use argv list (no shell) for cross-platform compat (Windows PowerShell).
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=BUILD_ROOT, capture_output=True,
        # Binary mode — don't decode, just hash the bytes
    )
    if result.returncode != 0:
        # Not in HEAD (untracked / new file in working tree)
        return None
    h = hashlib.sha256()
    h.update(result.stdout)
    return h.hexdigest()


def _batch_sha256_git_head(rel_paths: list[str]) -> dict[str, str | None]:
    """Calculate SHA256 of MULTIPLE files in git HEAD — via ONE git process.

    Uses `git cat-file --batch` (batch mode) to read many blobs in a single
    subprocess. This is CRITICAL for performance on Windows where spawning
    a new process for each file costs ~100ms × 5000 files = 8+ minutes.
    With batch mode, all 5000 files are read in ~2-5 seconds total.

    Args:
      rel_paths: list of paths relative to BUILD_ROOT

    Returns:
      dict { rel_path: sha256_hex or None (if not in HEAD) }
    """
    if not rel_paths:
        return {}

    # Spawn ONE git process, feed it paths via stdin, read responses from stdout.
    # Format of git cat-file --batch input: one object name per line.
    # Format of output per object:
    #   <oid> <type> <size>\n
    #   <content>\n
    # If object missing: <path> missing\n
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=BUILD_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdin is None or proc.stdout is None:
        return {p: None for p in rel_paths}

    # Send all paths at once
    input_data = "\n".join(f"HEAD:{p}" for p in rel_paths) + "\n"
    try:
        proc.stdin.write(input_data.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # git may have exited early (e.g., bad path) — handle below

    result: dict[str, str | None] = {}
    for path in rel_paths:
        # Read header line: "<oid> <type> <size>" or "<path> missing"
        header = proc.stdout.readline()
        if not header:
            # git exited unexpectedly — remaining paths are None
            result[path] = None
            continue
        header_str = header.decode("utf-8", errors="replace").strip()
        if header_str.endswith(" missing"):
            # Not in HEAD (untracked / new file)
            result[path] = None
            continue
        # Parse header: "<oid> blob <size>"
        parts = header_str.split()
        if len(parts) < 3:
            result[path] = None
            continue
        try:
            size = int(parts[2])
        except ValueError:
            result[path] = None
            continue
        # Read exactly <size> bytes of blob content
        content = proc.stdout.read(size)
        # Read trailing newline after content
        proc.stdout.read(1)
        h = hashlib.sha256()
        h.update(content)
        result[path] = h.hexdigest()

    # Wait for git process to finish (should be quick — already read everything)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return result


def _git_ls_tree_head() -> dict[str, str | None]:
    """Get SHA1 (git blob hash) of ALL files in HEAD — via ONE git process.

    Uses `git ls-tree -r HEAD` — returns "<mode> <type> <sha1>\t<path>" for
    every file in the HEAD tree. ONE subprocess, returns in ~50ms even for
    huge repos (10x+ faster than batch cat-file, 1000x faster than per-file
    git show).

    The git SHA1 IS a hash of the blob content — it changes if content
    changes. Perfect for comparing baseline vs current state.

    NOTE: Returns git SHA1 (40 hex chars), NOT SHA256. This is a different
    hash algorithm than the old baseline format. State files using SHA256
    need migration (auto-detected — see migrate_state_format() below).

    Returns:
      dict { rel_path: git_sha1_hex (40 chars) or None if not in HEAD }
    """
    result = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD"],
        cwd=BUILD_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return {}
    tree: dict[str, str | None] = {}
    for line in result.stdout.splitlines():
        # Format: "<mode> <type> <sha1>\t<path>"
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) < 3:
            continue
        sha1 = parts[2]
        tree[path] = sha1
    return tree


def get_upstream_commit() -> str:
    """Get current HEAD commit hash."""
    stdout, _, _ = run("git rev-parse HEAD", cwd=BUILD_ROOT)
    return stdout


# ── State management ────────────────────────────────────────────────────────


def load_state() -> dict:
    """Load upstream state. Returns empty dict if not exists.

    Auto-migrates legacy SHA256 state → re-baseline (return empty dict).
    Detects SHA256 hashes by length: 64 chars = SHA256 (legacy), 40 = SHA1.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    # ── Auto-migrate: detect SHA256 hashes and trigger re-baseline ──
    # SHA256 = 64 hex chars, SHA1 = 40 hex chars.
    # If any hash in the state is 64 chars, it's a legacy SHA256 baseline
    # and we need to re-record it with the new SHA1 format.
    hash_format = state.get("hash_format", "sha256")  # default to legacy if missing
    if hash_format == "sha256":
        # Check if any hash is actually SHA256 (64 chars)
        for path, sha in state.get("patches", {}).items():
            if sha and len(sha) == 64:
                # Legacy SHA256 baseline — discard, force re-baseline
                print(f"  [INFO] Legacy SHA256 baseline detected — re-baselining with SHA1 (faster)")
                print(f"  [INFO] Old baseline: {len(state.get('patches', {}))} patches, "
                      f"{len(state.get('mods', {}))} mods")
                return {}
        for path, sha in state.get("mods", {}).items():
            if sha and len(sha) == 64:
                print(f"  [INFO] Legacy SHA256 baseline detected — re-baselining with SHA1 (faster)")
                return {}
    return state


def save_state(state: dict) -> None:
    """Save upstream state. Uses atomic write to prevent corruption."""
    _atomic_write_text(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text to a file atomically: write to temp, then os.replace.

    Prevents corruption if the process is killed (Ctrl+C, OOM) or disk fills
    up mid-write. os.replace() is atomic on POSIX.

    v0.3.1: delegates to common.atomic_write_text if available (single source
    of truth). Falls back to local implementation if common.py is missing
    (e.g. when Check.py is run from a different directory without sys.path
    manipulation — should never happen in practice).
    """
    if _atomic_write_text_impl is not None:
        _atomic_write_text_impl(path, content)
        return
    # Local fallback (legacy copy)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def collect_current_state() -> dict:
    """Collect current state: upstream commit + hash of all files we track.

    CRITICAL PERFORMANCE: Uses _git_ls_tree_head() — ONE `git ls-tree -r HEAD`
    subprocess that returns SHA1 of ALL files in HEAD in ~50ms. This is
    1000x faster than calling `git show HEAD:<path>` per file, and 10x faster
    than `git cat-file --batch` (which still has to read blob content).

    The git SHA1 IS a content hash — it changes iff blob content changes.
    Perfect for our use case (compare baseline vs current state to detect
    upstream changes that conflict with our overlay).

    NOTE: Old state files (pre-0.2.9) used SHA256. We auto-migrate by
    checking hash length: 64 chars = SHA256 (legacy), 40 chars = SHA1 (new).
    If old_state uses SHA256 and new_state uses SHA1, we re-baseline.

    Scans:
      - 00_Aavikko/01_Resources/Patches/ — for each file, get hash of upstream Resources/<path>
      - 00_Aavikko/01_Resources/Mods/    — for each file, check if upstream now has same path
      - 00_Aavikko/02_Content/Patches/   — for each .cs.patch/.xaml.patch, get hash of upstream Content.<path>
      - 00_Aavikko/02_Content/Mods/      — for each file, check if upstream now has same path
    """
    state = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "upstream_commit": get_upstream_commit(),
        "hash_format": "git_sha1",  # v2: was "sha256" pre-0.2.9
        "patches": {},  # path → git_sha1 of upstream file (from HEAD)
        "mods": {},     # path → git_sha1 of upstream file (or None if not in HEAD)
    }

    # ── Step 1: collect all paths to track ──
    patches_paths: list[str] = []
    mods_paths: list[str] = []

    # Resources/Patches/
    res_patches = RESOURCES_DIR / "Patches"
    if res_patches.exists():
        for f in res_patches.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(res_patches))
            patches_paths.append(f"Resources/{rel}")

    # Resources/Mods/
    res_mods = RESOURCES_DIR / "Mods"
    if res_mods.exists():
        for f in res_mods.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(res_mods))
            mods_paths.append(f"Resources/{rel}")

    # Content/Patches/
    content_patches = CONTENT_DIR / "Patches"
    if content_patches.exists():
        for f in content_patches.rglob("*"):
            if not f.is_file():
                continue
            name = f.name
            if name.endswith(".cs.patch"):
                upstream_rel = str(f.relative_to(content_patches)).replace(".cs.patch", ".cs")
            elif name.endswith(".xaml.patch"):
                upstream_rel = str(f.relative_to(content_patches)).replace(".xaml.patch", ".xaml")
            elif name.endswith(".xaml.cs.patch"):
                upstream_rel = str(f.relative_to(content_patches)).replace(".xaml.cs.patch", ".xaml.cs")
            else:
                continue
            patches_paths.append(upstream_rel)

    # Content/Mods/
    content_mods = CONTENT_DIR / "Mods"
    if content_mods.exists():
        for f in content_mods.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(content_mods))
            mods_paths.append(rel)

    # ── Step 2: ONE git ls-tree -r HEAD — get SHA1 of ALL files in HEAD ──
    # This is the magic — one subprocess, ~50ms, returns SHA1 for every file.
    all_head_hashes = _git_ls_tree_head()

    # ── Step 3: fill state from all_head_hashes ──
    for path in patches_paths:
        state["patches"][path] = all_head_hashes.get(path)
    for path in mods_paths:
        state["mods"][path] = all_head_hashes.get(path)

    return state


# ── Decisions management ───────────────────────────────────────────────────


def load_decisions() -> dict:
    """Load decisions from .conflict_decisions.yml. Returns empty dict if not exists.
    Validates format and warns about suspicious entries."""
    if not DECISIONS_FILE.exists():
        return {"patches": {}, "mods": {}}
    # Simple YAML parser (we control the format)
    decisions = {"patches": {}, "mods": {}}
    current_section = None
    current_path = None
    suspicious = []
    line_num = 0
    try:
        for line in DECISIONS_FILE.read_text(encoding="utf-8").splitlines():
            line_num += 1
            stripped = line.strip()
            if stripped == "patches:":
                current_section = "patches"
                current_path = None
                continue
            elif stripped == "mods:":
                current_section = "mods"
                current_path = None
                continue
            elif stripped.startswith("#") or not stripped:
                continue
            # Detect dict-style entries (user mistake: "path:" instead of "- path")
            if stripped and not stripped.startswith("- ") and ":" in stripped \
               and not stripped.startswith(("decision", "decided_at", "upstream_commit")):
                # Could be a path without leading "- "
                if current_section and not current_path:
                    suspicious.append(f"  line {line_num}: '{stripped}' — expected '- <path>'")
                    continue
            if stripped.startswith("- ") and current_section:
                # New path entry
                current_path = stripped[2:].strip()
                decisions[current_section][current_path] = {}
            elif ":" in stripped and current_path and current_section:
                key, val = stripped.split(":", 1)
                decisions[current_section][current_path][key.strip()] = val.strip()
    except OSError:
        return {"patches": {}, "mods": {}}

    # Warn about format issues
    if suspicious:
        print(f"\n[WARNING] .conflict_decisions.yml has {len(suspicious)} suspicious line(s):",
              file=sys.stderr)
        for s in suspicious[:5]:
            print(s, file=sys.stderr)
        print(f"\n  Correct format:", file=sys.stderr)
        print(f"    patches:", file=sys.stderr)
        print(f"      - Content.Shared/Foo/Bar.cs", file=sys.stderr)
        print(f"        decision: fr", file=sys.stderr)
        print(f"        decided_at: 2025-08-15T10:00:00+0300", file=sys.stderr)
        print(f"        upstream_commit: abc123...", file=sys.stderr)
        print(f"", file=sys.stderr)

    # Validate decision values
    # Patches: fr=force replace, u=updated, i=ignore, s=skip
    # Mods: k=keep, r=remove, i=ignore, s=skip
    # ('d' is a UI action — show detailed diff — NOT a real decision)
    valid_patches = {"fr", "u", "i", "s"}
    valid_mods = {"k", "r", "i", "s"}
    for path, info in decisions.get("patches", {}).items():
        d = info.get("decision", "")
        if d and d not in valid_patches:
            print(f"[WARNING] patches/{path}: invalid decision '{d}' "
                  f"(valid: {', '.join(sorted(valid_patches))})", file=sys.stderr)
    for path, info in decisions.get("mods", {}).items():
        d = info.get("decision", "")
        if d and d not in valid_mods:
            print(f"[WARNING] mods/{path}: invalid decision '{d}' "
                  f"(valid: {', '.join(sorted(valid_mods))})", file=sys.stderr)

    return decisions


def save_decisions(decisions: dict) -> None:
    """Save decisions to .conflict_decisions.yml."""
    lines = [
        "# Aavikko conflict decisions",
        f"# Updated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        "",
        "patches:",
    ]
    for path, info in sorted(decisions.get("patches", {}).items()):
        lines.append(f"  - {path}")
        for k, v in info.items():
            lines.append(f"    {k}: {v}")
    lines.extend(["", "mods:"])
    for path, info in sorted(decisions.get("mods", {}).items()):
        lines.append(f"  - {path}")
        for k, v in info.items():
            lines.append(f"    {k}: {v}")
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(DECISIONS_FILE, "\n".join(lines) + "\n")


# ── Conflict detection ─────────────────────────────────────────────────────


def detect_conflicts(old_state: dict, new_state: dict, decisions: dict) -> tuple[list, list]:
    """Detect conflicts between old and new state.
    Returns: (patches_conflicts, mods_conflicts)
    Each conflict is a dict with: path, old_sha, new_sha, type
    """
    patches_conflicts = []
    mods_conflicts = []

    old_commit = old_state.get("upstream_commit", "")
    new_commit = new_state.get("upstream_commit", "")

    # ── Type A: Patches/ conflicts — upstream file changed ──
    for path, old_sha in old_state.get("patches", {}).items():
        new_sha = new_state.get("patches", {}).get(path)

        # Skip if already decided with a resolving decision
        # Valid patch decisions: fr (force replace), u (updated), s (skip),
        # 'i' (ignore) means NOT resolved — conflict should reappear
        existing = decisions.get("patches", {}).get(path, {})
        if existing.get("decision") in ("fr", "u", "s"):
            # Already resolved — skip unless upstream changed AGAIN
            decided_commit = existing.get("upstream_commit", "")
            if decided_commit == new_commit:
                continue  # Same commit as when decided — still resolved

        if old_sha != new_sha and new_sha is not None:
            patches_conflicts.append({
                "path": path,
                "old_sha": old_sha,
                "new_sha": new_sha,
                "old_commit": old_commit,
                "new_commit": new_commit,
            })
        elif old_sha is not None and new_sha is None:
            # Upstream file was deleted
            patches_conflicts.append({
                "path": path,
                "old_sha": old_sha,
                "new_sha": None,
                "old_commit": old_commit,
                "new_commit": new_commit,
                "note": "upstream file was deleted",
            })

    # ── Type B: Mods/ conflicts — upstream now has same path ──
    for path, old_sha in old_state.get("mods", {}).items():
        new_sha = new_state.get("mods", {}).get(path)

        # Skip if already decided
        existing = decisions.get("mods", {}).get(path, {})
        if existing.get("decision") in ("k", "r"):
            decided_commit = existing.get("upstream_commit", "")
            if decided_commit == new_commit:
                continue

        # Type B: upstream DIDN'T have file before (old_sha=None) but NOW has it (new_sha != None)
        if old_sha is None and new_sha is not None:
            mods_conflicts.append({
                "path": path,
                "old_sha": None,
                "new_sha": new_sha,
                "old_commit": old_commit,
                "new_commit": new_commit,
            })
        # Type B2: upstream HAD our mod file (old_sha!=None) but changed it (new_sha != old_sha)
        # This happens when Content Mods are copied to upstream and upstream later changes them
        elif old_sha is not None and new_sha is not None and old_sha != new_sha:
            mods_conflicts.append({
                "path": path,
                "old_sha": old_sha,
                "new_sha": new_sha,
                "old_commit": old_commit,
                "new_commit": new_commit,
                "note": "upstream changed our mod file",
            })
        # Type B3: upstream had our mod file (old_sha!=None) but DELETED it (new_sha=None)
        elif old_sha is not None and new_sha is None:
            mods_conflicts.append({
                "path": path,
                "old_sha": old_sha,
                "new_sha": None,
                "old_commit": old_commit,
                "new_commit": new_commit,
                "note": "upstream deleted our mod file",
            })

    return patches_conflicts, mods_conflicts


# ── Temp snapshots for Patches/ conflicts ──────────────────────────────────


def create_temp_snapshots(conflict: dict) -> None:
    """Create temp snapshot files for a Patches/ conflict.

    For Resources/Patches/<path>:
      Creates in 00_Aavikko/01_Resources/Patches/<path>.{old.patched,conflict.upstream,conflict.patched}

    For Content/Patches/<path>.cs.patch:
      Creates in 00_Aavikko/02_Content/Patches/<path>.{old.patched,conflict.upstream,conflict.patched}
    """
    path = conflict["path"]  # e.g. "Content.Shared/Botany/Systems/PlantSystem.cs"

    # Determine if this is Resources or Content
    if path.startswith("Resources/"):
        patches_dir = RESOURCES_DIR / "Patches"
        rel = path[len("Resources/"):]
    else:
        patches_dir = CONTENT_DIR / "Patches"
        rel = path

    upstream_file = BUILD_ROOT / path

    # Find our patch file
    if path.startswith("Resources/"):
        patch_file = patches_dir / rel  # Full file replacement
    else:
        # rel already has .cs/.xaml extension — need to find matching .patch file
        # Try: PlantSystem.cs → PlantSystem.cs.patch (just append .patch)
        #       PlantSystem.xaml.cs → PlantSystem.xaml.cs.patch
        #       PlantSystem.xaml → PlantSystem.xaml.patch
        patch_file = None
        candidate = patches_dir / f"{rel}.patch"
        if candidate.exists():
            patch_file = candidate
        else:
            # Fallback: strip extension, try variants
            for old_ext, new_ext in [(".cs", ".cs.patch"), (".xaml.cs", ".xaml.cs.patch"), (".xaml", ".xaml.patch")]:
                if rel.endswith(old_ext):
                    candidate = patches_dir / f"{rel}{new_ext[len(old_ext):]}"
                    if candidate.exists():
                        patch_file = candidate
                        break

    # .conflict.upstream — copy of current upstream file (B)
    # Use git HEAD version, NOT working tree. Working tree may contain our
    # overlay if Apply was run (in which case the upstream content was overwritten
    # by our patch — we want to see the REAL upstream, not our modification).
    conflict_upstream = patches_dir / f"{rel}.conflict.upstream"
    conflict_upstream.parent.mkdir(parents=True, exist_ok=True)
    # Read upstream content from HEAD via git show (argv form, no shell — cross-platform)
    show_result = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=BUILD_ROOT, capture_output=True,
    )
    if show_result.returncode == 0:
        conflict_upstream.write_bytes(show_result.stdout)
        tag("SNAPSHOT", f"{conflict_upstream.relative_to(BUILD_ROOT)} (upstream B from HEAD)", indent=4, color=cyan)
    elif upstream_file.exists():
        # Fallback to working tree (rare: file is new and not yet committed upstream)
        shutil.copy2(upstream_file, conflict_upstream)
        tag("SNAPSHOT", f"{conflict_upstream.relative_to(BUILD_ROOT)} (upstream B from working tree — fallback)", indent=4, color=yellow)

    # .old.patched — what our patched version looked like (A')
    old_state = load_state()
    old_sha = conflict.get("old_sha")
    if old_sha and upstream_file.exists():
        # We can't easily reconstruct old upstream (A) — but we can apply our patch
        # to current upstream and see if it works, or just note what our patch does.
        # For now: if we have a patch file, try to apply it to a temp copy of upstream
        # to get A' (old patched version).
        # If patch doesn't apply (context changed), skip.
        if patch_file and patch_file.exists():
            # For Content patches (.cs.patch): try git apply on temp copy
            if str(patch_file).endswith(".patch"):
                # Create temp copy, try to apply
                old_patched = patches_dir / f"{rel}.old.patched"
                shutil.copy2(upstream_file, old_patched)
                # Try to reverse-apply to get back to old state... complex.
                # Simpler: just note that .old.patched is not available for .cs.patch
                # (would need to checkout old commit, apply patch, save result)
                # For now, leave a note
                old_patched.write_text(
                    f"# old.patched not available for .cs.patch conflicts.\n"
                    f"# To see old patched version:\n"
                    f"#   git checkout {conflict.get('old_commit','?')} -- {path}\n"
                    f"#   git apply {patch_file}\n"
                    f"#   # file is now A' (old patched)\n",
                    encoding="utf-8"
                )
                tag("SNAPSHOT", f"{old_patched.relative_to(BUILD_ROOT)} (note: see comment)", indent=4, color=cyan)
            else:
                # For Resources Patches (full file): our patch IS the old patched version
                old_patched = patches_dir / f"{rel}.old.patched"
                shutil.copy2(patch_file, old_patched)
                tag("SNAPSHOT", f"{old_patched.relative_to(BUILD_ROOT)} (old patched A')", indent=4, color=cyan)
        else:
            warn(f"No patch file found for {path}")
    else:
        warn("Cannot create old.patched — upstream file or old sha missing")

    # .conflict.patched — attempt to apply our patch to new upstream (B')
    if patch_file and patch_file.exists() and upstream_file.exists():
        conflict_patched = patches_dir / f"{rel}.conflict.patched"
        conflict_patched.parent.mkdir(parents=True, exist_ok=True)

        if str(patch_file).endswith(".patch"):
            # For .cs.patch: copy upstream, try git apply
            shutil.copy2(upstream_file, conflict_patched)
            _, _, rc = run(f"git apply --check {patch_file}", cwd=BUILD_ROOT)
            if rc == 0:
                # Apply to the temp copy... actually git apply works on tracked files.
                # Instead: apply to upstream temporarily, copy result, revert.
                # Too risky. Just note if it would apply.
                conflict_patched.write_text(
                    f"# Patch CAN be applied to new upstream (git apply --check passed).\n"
                    f"# To get B' (patched new upstream):\n"
                    f"#   git apply {patch_file}\n"
                    f"#   cp {path} {conflict_patched}\n"
                    f"#   git checkout -- {path}\n",
                    encoding="utf-8"
                )
                tag("SNAPSHOT", f"{conflict_patched.relative_to(BUILD_ROOT)} (patch applies OK)", indent=4, color=green)
            else:
                conflict_patched.write_text(
                    f"# Patch CANNOT be applied to new upstream (context changed).\n"
                    f"# Manual update required.\n",
                    encoding="utf-8"
                )
                tag("SNAPSHOT", f"{conflict_patched.relative_to(BUILD_ROOT)} (patch FAILS — manual update)", indent=4, color=red)
        else:
            # For Resources Patches (full file): our patch IS the patched version
            shutil.copy2(patch_file, conflict_patched)
            tag("SNAPSHOT", f"{conflict_patched.relative_to(BUILD_ROOT)} (our patched B')", indent=4, color=cyan)


# ── Interactive prompt ─────────────────────────────────────────────────────


def prompt_patches_conflict(conflict: dict, non_interactive: bool) -> str:
    """Ask developer how to handle a Patches/ conflict.
    Returns: 'fr', 'u', 'i', or 's' (skip)
    """
    path = conflict["path"]
    print()
    divider()
    print(f"{bold(red('CONFLICT'))} {dim('(Patches/)')}: {bold(path)}")
    print(f"  Upstream changed: {conflict.get('old_commit','?')[:8]} → {conflict.get('new_commit','?')[:8]}")
    if conflict.get("note"):
        info(conflict['note'])

    # Show upstream diff (first 15 lines)
    if conflict.get("old_commit") and conflict.get("new_commit"):
        old_c = conflict['old_commit']
        new_c = conflict['new_commit']
        # Validate commit hashes to prevent shell injection
        if not (COMMIT_RE.match(old_c) and COMMIT_RE.match(new_c)):
            print(f"\n  [WARN] Invalid commit hash in state file — skipping diff")
        else:
            path_q = shlex.quote(path)
            diff_stdout, _, _ = run(
                f"git diff {old_c}..{new_c} -- {path_q}",
                cwd=BUILD_ROOT
            )
            if diff_stdout:
                diff_lines = diff_stdout.splitlines()[:15]
                print(f"\n  --- Upstream diff (first 15 lines) ---")
                for line in diff_lines:
                    print(f"  {line}")
                if len(diff_stdout.splitlines()) > 15:
                    print(f"  ... ({len(diff_stdout.splitlines()) - 15} more lines)")

    if non_interactive:
        print(f"\n  {dim('[non-interactive] Default: ignore')}")
        return "i"

    print(f"\n  {bold('Options:')}")
    print(f"    {green('[fr]')} force replace — use our patch, forget upstream change")
    print(f"    {green('[u]')}  update — I updated the patch (run Generate.py)")
    print(f"    {yellow('[i]')}  ignore — skip for now, will reappear next Check")
    print(f"    {cyan('[d]')}  detailed — show full diff")
    print(f"    {red('[s]')}  skip — don't apply this patch at all")

    while True:
        choice = input(f"\n  Decision for {bold(Path(path).name)}: ").strip().lower()
        if choice in ("fr", "u", "i", "s"):
            return choice
        if choice == "d":
            # Show full diff
            if conflict.get("old_commit") and conflict.get("new_commit"):
                diff_stdout, _, _ = run(
                    f"git diff {conflict['old_commit']}..{conflict['new_commit']} -- {path}",
                    cwd=BUILD_ROOT
                )
                print(f"\n  {dim('--- Full upstream diff ---')}")
                for line in diff_stdout.splitlines():
                    print(f"  {line}")
            continue
        warn("Invalid choice. Use: fr, u, i, d, s", indent=4)


def prompt_mods_conflict(conflict: dict, non_interactive: bool) -> str:
    """Ask developer how to handle a Mods/ conflict.
    Returns: 'k', 'r', 'i', or 's'
    """
    path = conflict["path"]
    print()
    divider()
    print(f"{bold(yellow('CONFLICT'))} {dim('(Mods/)')}: {bold(path)}")
    print(f"  Upstream now has this file (didn't before)")
    print(f"  Our mod: Aavikko.*/Mods/{path}")

    upstream_file = BUILD_ROOT / path
    if upstream_file.exists():
        size = upstream_file.stat().st_size
        print(f"  Upstream size: {size} bytes")

    if non_interactive:
        print(f"\n  {dim('[non-interactive] Default: ignore')}")
        return "i"

    print(f"\n  {bold('Options:')}")
    print(f"    {green('[k]')} keep our mod — override upstream")
    print(f"    {green('[r]')} remove our mod — use upstream version")
    print(f"    {yellow('[i]')} ignore — skip for now")
    print(f"    {cyan('[d]')} detailed — show file info")

    while True:
        choice = input(f"\n  Decision for {bold(Path(path).name)}: ").strip().lower()
        if choice in ("k", "r", "i", "s"):
            return choice
        if choice == "d":
            our_file = None
            if path.startswith("Resources/"):
                our_file = RESOURCES_DIR / "Mods" / path[len("Resources/"):]
            else:
                our_file = CONTENT_DIR / "Mods" / path
            if our_file and our_file.exists():
                print(f"  Our mod size: {our_file.stat().st_size} bytes")
            if upstream_file.exists():
                print(f"  Upstream size: {upstream_file.stat().st_size} bytes")
            continue
        warn("Invalid choice. Use: k, r, i, d, s", indent=4)


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Check for upstream conflicts")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Don't ask, default 'ignore' for all conflicts")
    parser.add_argument("--baseline", action="store_true",
                        help="Force re-record baseline (forget all conflicts)")
    parser.add_argument("--apply-check", action="store_true",
                        help="Only check if Apply.py can run (no unresolved conflicts)")
    parser.add_argument("--json", action="store_true",
                        help="Output conflicts as JSON (for VS Code extension)")
    args = parser.parse_args()

    # --json mode: detect conflicts, output as JSON, no interactive prompts
    if args.json:
        import json as json_mod
        old_state = load_state() if not args.baseline else {}
        if not old_state:
            new_state = collect_current_state()
            save_state(new_state)
            print(json_mod.dumps({
                "baseline_recorded": True,
                "upstream_commit": new_state.get("upstream_commit", ""),
                "patches_tracked": len(new_state.get("patches", {})),
                "mods_tracked": len(new_state.get("mods", {})),
                "conflicts": {"patches": [], "mods": []},
            }, indent=2))
            return

        decisions = load_decisions()
        new_state = collect_current_state()
        patches_conflicts, mods_conflicts = detect_conflicts(old_state, new_state, decisions)

        # Include existing decisions in output
        existing_decisions = {
            "patches": {k: v for k, v in decisions.get("patches", {}).items()},
            "mods": {k: v for k, v in decisions.get("mods", {}).items()},
        }

        print(json_mod.dumps({
            "baseline_recorded": False,
            "upstream_commit": new_state.get("upstream_commit", ""),
            "old_commit": old_state.get("upstream_commit", ""),
            "conflicts": {
                "patches": patches_conflicts,
                "mods": mods_conflicts,
            },
            "decisions": existing_decisions,
        }, indent=2, ensure_ascii=False))
        return

    header("Aavikko Conflict Check", "detect upstream changes")

    # Load old state
    old_state = load_state() if not args.baseline else {}
    decisions = load_decisions()

    if not old_state:
        print()
        info("No previous state found — recording baseline.")
        info("This is the starting point. No conflicts will be reported.")
        new_state = collect_current_state()
        save_state(new_state)
        print()
        kv("Upstream commit", new_state['upstream_commit'][:12])
        kv("Tracked patches", len(new_state.get('patches', {})))
        kv("Tracked mods",    len(new_state.get('mods', {})))
        print()
        info(f"State saved: {STATE_FILE.relative_to(BUILD_ROOT)}")
        success_banner(
            "Baseline recorded",
            next_step="Run Check.py again after `git pull` to detect conflicts.",
        )
        return

    # --apply-check mode: just verify no unresolved conflicts
    if args.apply_check:
        if not STATE_FILE.exists():
            print(f"\n[ERROR] No .upstream_state.json found.", file=sys.stderr)
            print(f"  Run Check.py (without --apply-check) first to record baseline.", file=sys.stderr)
            sys.exit(1)
        new_state = collect_current_state()
        patches_conflicts, mods_conflicts = detect_conflicts(old_state, new_state, decisions)
        unresolved = len(patches_conflicts) + len(mods_conflicts)
        if unresolved > 0:
            print(f"\n[BLOCKED] {unresolved} unresolved conflict(s) — Apply.py cannot run.")
            print(f"  Run: python3 {SCRIPT_DIR.name}/Check.py")
            print(f"  Resolve all conflicts, then re-run Apply.py.")
            sys.exit(1)
        else:
            print("\n[OK] No unresolved conflicts. Apply.py can run.")
            return

    # Normal mode: detect and resolve conflicts
    section(1, 2, "Comparing upstream state")
    kv("Old commit", old_state.get('upstream_commit', '?')[:12])
    new_state = collect_current_state()
    kv("New commit", new_state['upstream_commit'][:12])

    patches_conflicts, mods_conflicts = detect_conflicts(old_state, new_state, decisions)

    total = len(patches_conflicts) + len(mods_conflicts)
    print()
    kv("Patches/ conflicts", len(patches_conflicts))
    kv("Mods/ conflicts",    len(mods_conflicts))

    if total == 0:
        print()
        ok("No conflicts detected. Upstream is compatible with our overlay.")
        # Update state to latest commit (so we track from here)
        save_state(new_state)
        info(f"State updated to {new_state['upstream_commit'][:12]}")
        return

    # ── Resolve conflicts ──
    section(2, 2, f"Resolving {total} conflict(s)")

    for conflict in patches_conflicts:
        # Create temp snapshots
        print()
        info(f"Creating temp snapshots for {conflict['path']}...")
        create_temp_snapshots(conflict)

        # Prompt
        decision = prompt_patches_conflict(conflict, args.non_interactive)
        decisions.setdefault("patches", {})[conflict["path"]] = {
            "decision": decision,
            "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "upstream_commit": conflict["new_commit"],
        }
        print(f"  {dim('→')} {bold(decision)}")

    for conflict in mods_conflicts:
        decision = prompt_mods_conflict(conflict, args.non_interactive)
        decisions.setdefault("mods", {})[conflict["path"]] = {
            "decision": decision,
            "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "upstream_commit": conflict["new_commit"],
        }
        print(f"  {dim('→')} {bold(decision)}")

        # Act on decision
        path = conflict["path"]  # e.g. "Resources/Audio/Voice/Human/male_sigh1.ogg"
        if path.startswith("Resources/"):
            mods_base = RESOURCES_DIR / "Mods"
            patches_base = RESOURCES_DIR / "Patches"
            rel = path[len("Resources/"):]
        else:
            mods_base = CONTENT_DIR / "Mods"
            patches_base = CONTENT_DIR / "Patches"
            rel = path

        src = mods_base / rel
        dst = patches_base / rel

        if decision == "k":
            # Keep our mod → move from Mods/ to Patches/ (it's now an override, not a new file)
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                ok(f"Mods/{rel} → Patches/{rel} (now tracked as override)")
            else:
                warn(f"Mods/{rel} not found (already moved?)")
        elif decision == "r":
            # Remove our mod → delete from Mods/ (upstream version will be used)
            if src.exists():
                src.unlink()
                tag("DELETED", f"Mods/{rel} (upstream version will be used)", indent=2, color=yellow)
            else:
                warn(f"Mods/{rel} not found (already deleted?)")

    # Save decisions
    save_decisions(decisions)
    info(f"Decisions saved: {DECISIONS_FILE.relative_to(BUILD_ROOT)}")

    # Update state ONLY if all conflicts resolved (no 'ignore' decisions)
    # If there are ignored conflicts, keep old state so they reappear next Check
    has_ignored = any(
        v.get("decision") == "i"
        for v in decisions.get("patches", {}).values()
    ) or any(
        v.get("decision") == "i"
        for v in decisions.get("mods", {}).values()
    )

    if has_ignored:
        warn("State NOT updated — ignored conflicts remain")
        info("Ignored conflicts will reappear on next Check.py run.")
    else:
        save_state(new_state)
        ok(f"State updated to {new_state['upstream_commit'][:12]}")

    # Summary
    resolved = sum(1 for v in decisions.get("patches", {}).values()
                   if v.get("decision") in ("fr", "u", "s"))
    ignored = sum(1 for v in decisions.get("patches", {}).values()
                  if v.get("decision") == "i")
    if ignored > 0:
        fail_banner(
            f"Check complete — {ignored} conflict(s) ignored",
            hints=[
                f"Resolved: {resolved}, ignored: {ignored}",
                "Apply.py will refuse to run until all conflicts are resolved.",
            ],
        )
    else:
        success_banner(
            f"Check complete — {resolved} conflict(s) resolved",
            next_step="All conflicts resolved. Apply.py can run.",
        )


if __name__ == "__main__":
    main()