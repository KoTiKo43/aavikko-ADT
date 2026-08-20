#!/usr/bin/env python3
"""common.py — shared utilities for Aavikko Patcher scripts.

Consolidates cross-cutting helpers that were previously duplicated in
Apply.py, Check.py, Clear.py, etc.  Keeping them in one place makes bug
fixes and platform-specific tweaks (Windows cp1251, fcntl/msvcrt locking,
git index.lock retries) a one-file change instead of a 4-file change.

Public surface:
  - Constants:   SCRIPT_DIR, BUILD_ROOT, RESOURCES_DIR, CONTENT_DIR, ...
  - run()         cross-platform subprocess runner (argv list OR shell string)
  - run_git_with_lock_retry()   git index.lock retry wrapper
  - atomic_write_text()          crash-safe file write
  - safe_resolve_under()         path-traversal guard for manifest.yml
  - force_utf8_stdio()           reconfigure stdout/stderr to UTF-8
  - timing helpers: step_timer, print_timing_summary

Designed to be a *drop-in* import — no side effects on import, no behavior
change for existing callers (function signatures are identical to the
inlined copies they replace).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# ── Module-level paths (every script shares these) ────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_ROOT = SCRIPT_DIR.parent.parent.parent  # 00_Patcher/ → 00_Aavikko/00_Modding/ → build_root/

RESOURCES_DIR = BUILD_ROOT / "00_Aavikko/01_Resources"
MODS_DIR = RESOURCES_DIR / "Mods"
PATCHES_DIR = RESOURCES_DIR / "Patches"
MANIFEST = RESOURCES_DIR / "manifest.yml"
CONTENT_DIR = BUILD_ROOT / "00_Aavikko/02_Content"
CS_MODS_DIR = CONTENT_DIR / "Mods"
CS_PATCHES_DIR = CONTENT_DIR / "Patches"
ROBUST_DIR = BUILD_ROOT / "RobustToolbox"
ROBUST_OVERLAY_DIR = BUILD_ROOT / "00_Aavikko/03_RobustToolbox"
ROBUST_MODS_DIR = ROBUST_OVERLAY_DIR / "Mods"
ROBUST_PATCHES_DIR = ROBUST_OVERLAY_DIR / "Patches"

APPLIED_FILE = SCRIPT_DIR / ".applied"
LOCK_FILE = SCRIPT_DIR / ".apply.lock"
STATE_FILE = SCRIPT_DIR / ".upstream_state.json"
DECISIONS_FILE = SCRIPT_DIR / ".conflict_decisions.yml"
SYMLINK_STATE_FILE = SCRIPT_DIR / ".symlinks.json"

# Temp snapshot suffixes created by Check.py — must NOT be copied to Resources/
TEMP_SNAPSHOT_SUFFIXES = (".conflict.upstream", ".old.patched", ".conflict.patched")


# ── stdio UTF-8 ────────────────────────────────────────────────────────────
def force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 with errors='replace'.

    Windows default encoding is cp1251 which can't encode Unicode characters
    like →, —, ✓, ⚠ used in print() statements. Without this, scripts crash
    with UnicodeEncodeError on Windows PowerShell.

    Safe to call multiple times; no-op if reconfigure fails (e.g. when stdout
    is redirected to a file that's already UTF-8).
    """
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except (OSError, ValueError):
            pass  # Fails on some streams (e.g., redirected to file) — ignore


# ── Subprocess helpers ─────────────────────────────────────────────────────
def run(cmd, cwd: Path | None = None, timeout: int | None = None) -> tuple[str, str, int]:
    """Run a command. Accepts either a string (shell-style) OR an argv list.

    Cross-platform: never uses shell=True. If `cmd` is a string, it's split
    via shlex.split (which can BREAK on Windows paths with backslashes if
    they're not properly quoted). For safety, prefer passing an argv list:

        run(["git", "apply", "--check", str(patch_path)])  # SAFE
        run("git rev-parse HEAD")                          # OK (no paths)

    The argv list form is 100% cross-platform — no shell, no quoting,
    no backslash interpretation. Always prefer it for commands with paths.
    """
    if isinstance(cmd, str):
        try:
            argv = shlex.split(cmd)
        except ValueError:
            # Fallback for edge cases (unbalanced quotes) — use shell
            result = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        if not argv:
            return "", "", 0
    else:
        argv = list(cmd)
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def run_git_with_lock_retry(cmd, cwd: Path | None = None,
                            max_retries: int = 5, retry_delay: float = 1.0,
                            timeout: int | None = None) -> tuple[str, str, int]:
    """Run a git command with retry on index.lock conflict.

    Accepts either a string OR an argv list (delegates to run()).

    Git returns rc=128 with message "Unable to create '.git/index.lock': File exists"
    when another git process (Status.py polling, VS Code Source Control view, etc.)
    is running concurrently. This is a common race condition when:
      - VS Code extension polls Status.py every 15 sec (which calls git status)
      - Apply.py runs git apply (which writes to .git/index.lock)
      - User has VS Code Source Control panel open

    Strategy: detect index.lock error, wait 1 sec, retry. Up to 5 times.
    """
    last_stdout, last_stderr, last_rc = "", "", 0
    for attempt in range(max_retries):
        last_stdout, last_stderr, last_rc = run(cmd, cwd=cwd, timeout=timeout)
        if last_rc == 0:
            return last_stdout, last_stderr, last_rc
        # Check if this is the index.lock error
        if "index.lock" not in last_stderr and "index.lock" not in (last_stdout or ""):
            # Different error — don't retry, return immediately
            return last_stdout, last_stderr, last_rc
        # index.lock conflict — wait and retry
        if attempt < max_retries - 1:
            print(f"  [INFO] git index.lock busy, retry {attempt+1}/{max_retries} in {retry_delay}s...",
                  file=sys.stderr, flush=True)
            time.sleep(retry_delay)
    return last_stdout, last_stderr, last_rc


# ── Filesystem helpers ─────────────────────────────────────────────────────
def atomic_write_text(path: Path, content: str) -> None:
    """Write text to a file atomically: write to temp, then os.replace.

    Prevents corruption if the process is killed (Ctrl+C, OOM) or disk fills
    up mid-write. os.replace() is atomic on POSIX.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        # Cleanup temp file on failure
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def safe_resolve_under(under: Path, rel_path: str) -> Path | None:
    """Resolve rel_path under `under` directory, refusing path traversal.
    Returns the resolved Path, or None if rel_path escapes `under` or targets root."""
    if not rel_path or not rel_path.strip():
        return None
    cleaned = rel_path.lstrip("/")
    if cleaned in (".", "./", ""):
        return None  # Don't allow targeting the root directory itself
    candidate = (under / cleaned).resolve()
    under_resolved = under.resolve()
    try:
        candidate.relative_to(under_resolved)
    except ValueError:
        return None
    if candidate == under_resolved:
        return None  # Don't allow deleting the root itself
    return candidate


# ── Timing helpers ────────────────────────────────────────────────────────
_timings: list[tuple[str, float]] = []


@contextmanager
def step_timer(step_name: str):
    """Context manager for timing a step.

    Usage:
        with step_timer("Copy Patches/ → Resources/"):
            ...code...

    Logs elapsed time to stderr in format:
        [TIMING] Copy Patches/ → Resources/ : 1.234s
    """
    start = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - start
        _timings.append((step_name, elapsed))
        print(f"  [TIMING] {step_name}: {elapsed:.3f}s", file=sys.stderr, flush=True)


def print_timing_summary() -> None:
    """Print final summary of all timed steps (collected via step_timer)."""
    if not _timings:
        return
    print(f"\n--- Timing summary ---", file=sys.stderr)
    total = sum(t for (_, t) in _timings)
    for (name, elapsed) in _timings:
        pct = (elapsed / total * 100) if total > 0 else 0
        # Bar chart — each █ = 5% (max 20 chars)
        bar_len = min(20, int(pct / 5))
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(f"  {bar} {elapsed:6.3f}s  {pct:5.1f}%  {name}", file=sys.stderr)
    print(f"  {'─' * 50}", file=sys.stderr)
    print(f"  Total: {total:.3f}s", file=sys.stderr)
    # Reset timings so re-entry doesn't double-print (e.g. when Apply.py calls
    # Clear.py and then runs its own steps)
    _timings.clear()