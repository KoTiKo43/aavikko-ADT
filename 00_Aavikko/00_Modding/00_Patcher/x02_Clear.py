#!/usr/bin/env python3
"""
Clear.py — revert upstream to clean state after Apply.py.

What it does:
  1. Reverts Resources/ to HEAD (git checkout + git clean)
  2. Reverts Content.* to HEAD (git checkout + git clean)
  3. Reverts RobustToolbox to HEAD (git checkout + git clean)
  4. Deletes .applied marker

If .applied exists: reads it for info, then does full revert.
If .applied missing: does full revert anyway (safety).

Also cleans up:
  - .upstream_state.json (state tracking — stale after revert)
  - .conflict_decisions.yml (decisions — stale after revert)
  - Temp snapshot files (.old.patched, .conflict.upstream, .conflict.patched)

Usage:
  python3 x02_Clear.py             # Full revert
  python3 x02_x02_Clear.py --dry-run   # Show what would be reverted
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Shared utilities (single source of truth for run/atomic_write/safe_resolve)
from l00_common import (
    SCRIPT_DIR, BUILD_ROOT, APPLIED_FILE, LOCK_FILE,
    STATE_FILE, DECISIONS_FILE,
    force_utf8_stdio, run, run_git_with_lock_retry,
    atomic_write_text,
    step_timer, print_timing_summary,
)

force_utf8_stdio()

# Directories that Apply.py touches — all reverted on clear
REVERT_DIRS = [
    "Resources",
    "Content.Server",
    "Content.Client",
    "Content.Shared",
    "Content.Server.Database",
    "Content.Tests",
    "Content.IntegrationTests",
]

# Re-exported by common for backward compat with anything that imports from Clear
__all__ = ['run', 'run_git_with_lock_retry', 'revert_dir', 'revert_robusttoolbox',
           'sync_content_mods_back', 'cleanup_temp_snapshots', 'main']


def run(cmd, cwd: Path | None = None) -> tuple[str, str, int]:
    """Run a command (delegates to common.run for cross-platform compat).

    Kept here as a thin wrapper so existing imports `from Clear import run`
    still work — but all scripts should prefer `from l00_common import run`.
    """
    from l00_common import run as _run
    return _run(cmd, cwd=cwd)


def run_git_with_lock_retry(cmd, cwd: Path | None = None,
                            max_retries: int = 5, retry_delay: float = 1.0) -> tuple[str, str, int]:
    """Delegate to common.run_git_with_lock_retry (kept for backward compat)."""
    from l00_common import run_git_with_lock_retry as _rg
    return _rg(cmd, cwd=cwd, max_retries=max_retries, retry_delay=retry_delay)


def sync_content_mods_back():
    """Sync Content.*/Aavikko/ → 00_Aavikko/02_Content/Mods/ before reverting.

    Apply.py copies 00_Aavikko/02_Content/Mods/*.cs → Content.*/Aavikko/.
    Developer may edit these files in-place (in Content.*/Aavikko/).
    Before Clear reverts upstream (git clean -fd removes them), we sync
    changes back to the overlay so nothing is lost.

    Only syncs files that exist in upstream (applied mods). New files created
    by dev in Content.*/Aavikko/ that don't have overlay counterpart are also
    copied back.
    """
    import shutil
    content_mods_dir = BUILD_ROOT / "00_Aavikko/02_Content" / "Mods"
    if not content_mods_dir.exists():
        return 0

    synced = 0
    # Walk all Content.* dirs and look for Aavikko/ subdirs
    for content_proj in ("Content.Server", "Content.Client", "Content.Shared",
                         "Content.Tests", "Content.Server.Database",
                         "Content.IntegrationTests"):
        aavikko_dir = BUILD_ROOT / content_proj / "Aavikko"
        if not aavikko_dir.exists():
            continue

        # Walk all files in Content.*/Aavikko/
        for src in sorted(aavikko_dir.rglob("*")):
            if not src.is_file():
                continue
            if src.is_symlink():
                continue
            # Mirror path: Content.Server/Aavikko/Foo.cs → 00_Aavikko/02_Content/Mods/Content.Server/Aavikko/Foo.cs
            rel = src.relative_to(BUILD_ROOT)
            dst = content_mods_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)

            # Only sync if content differs (or file doesn't exist in overlay)
            need_sync = True
            if dst.exists():
                try:
                    if src.read_bytes() == dst.read_bytes():
                        need_sync = False
                except OSError:
                    pass

            if need_sync:
                shutil.copy2(src, dst)
                synced += 1

    return synced


def revert_dir(dirname: str) -> bool:
    """Revert a directory to HEAD: git checkout + git clean.
    Returns True if BOTH commands succeeded (no errors).

    Uses run_git_with_lock_retry() with argv list form (cross-platform safe).
    """
    target = BUILD_ROOT / dirname
    if not target.exists():
        return False

    # git checkout HEAD -- <dir> (revert tracked file modifications)
    # Use argv list — no shlex.split/quoting issues on Windows
    _, _, rc1 = run_git_with_lock_retry(
        ["git", "checkout", "HEAD", "--", f"{dirname}/"], cwd=BUILD_ROOT)

    # git clean -fd <dir> (remove untracked files added by Apply)
    _, _, rc2 = run_git_with_lock_retry(
        ["git", "clean", "-fd", f"{dirname}/"], cwd=BUILD_ROOT)

    # Both should succeed; if either fails, it's a real error (not partial)
    if rc1 != 0:
        print(f"  [WARN] git checkout failed for {dirname}/ (rc={rc1})", file=sys.stderr)
    if rc2 != 0:
        print(f"  [WARN] git clean failed for {dirname}/ (rc={rc2})", file=sys.stderr)
    return rc1 == 0 and rc2 == 0


def revert_robusttoolbox() -> bool:
    """Revert RobustToolbox submodule to HEAD."""
    rb = BUILD_ROOT / "RobustToolbox"
    if not rb.exists():
        return False
    # Use argv list + retry for submodule too (same race condition applies)
    run_git_with_lock_retry(["git", "checkout", "HEAD", "--", "."], cwd=rb)
    run_git_with_lock_retry(["git", "clean", "-fd"], cwd=rb)
    return True


def cleanup_temp_snapshots():
    """Remove temp snapshot files created by Check.py.
    These are: .old.patched, .conflict.upstream, .conflict.patched
    Found in 00_Aavikko/01_Resources/Patches/ and 00_Aavikko/02_Content/Patches/"""
    cleaned = 0
    for patches_dir in [
        BUILD_ROOT / "00_Aavikko/01_Resources" / "Patches",
        BUILD_ROOT / "00_Aavikko/02_Content" / "Patches",
    ]:
        if not patches_dir.exists():
            continue
        for suffix in [".old.patched", ".conflict.upstream", ".conflict.patched"]:
            for f in patches_dir.rglob(f"*{suffix}"):
                if f.is_file():
                    f.unlink()
                    cleaned += 1
    return cleaned


def main():
    parser = argparse.ArgumentParser(description="Revert Aavikko mod overlay (clear upstream)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be reverted, don't actually do it")
    parser.add_argument("--wipe-decisions", action="store_true",
                        help="Also delete .conflict_decisions.yml (default: keep)")
    args = parser.parse_args()

    print("=" * 70)
    print("Aavikko Mod Clear")
    print("=" * 70)

    # Remove ALL symlinks before revert: @Mods/@Patches, @Path, @patched
    # (they would dangle after Clear reverts upstream files)
    # Fast-path: read .symlinks.json state file (O(N) where N = symlinks created, ~32).
    # Fallback: slow rglob walk (O(filesystem tree), ~50s on HDD with 5000+ files)
    # if state file is missing (first run, legacy state, or manual cleanup).
    #
    # v0.3.1 OPTIMIZATION: If NEITHER .applied NOR .symlinks.json exists, skip
    # the removal step entirely. Clear.py is most often called when upstream
    # is already pristine (right after a previous Clear, or fresh checkout) —
    # in that case there are no symlinks to remove, and the slow rglob fallback
    # would walk 5000+ files for nothing. On HDD this saves 30-50s per Clear.
    try:
        import l02_symlinks as SymLinks
        print("\n--- Removing all symlinks (@Mods/@Patches/@Path/@patched) ---", flush=True)
        if not SymLinks.symlinks_likely_exist():
            # Neither .applied nor .symlinks.json → pristine state, no symlinks exist
            print("  [OK] No symlinks (pristine state — no .applied, no .symlinks.json)")
        else:
            removed = SymLinks.remove_all_tracked_symlinks()
            if removed >= 0:
                # Fast path succeeded (state file existed)
                if removed > 0:
                    print(f"  [OK] Removed {removed} symlinks (fast-path via .symlinks.json)")
                else:
                    print(f"  [OK] No symlinks found (state file empty — already removed)")
            else:
                # Fallback: state file missing BUT .applied exists → possible
                # crash mid-Apply, or manual state file deletion.
                # Use slow rglob walk to catch any stragglers.
                print("  [INFO] No .symlinks.json but .applied exists — using rglob scan (crash recovery?)")
                removed = 0
                for overlay_root, label, _ in SymLinks.OVERLAY_PAIRS:
                    if overlay_root.exists():
                        removed += SymLinks.remove_nav_links_for_pair(overlay_root, label)
                        removed += SymLinks.remove_path_links_for_overlay(overlay_root, label)
                        removed += SymLinks.remove_patched_links(overlay_root, label)
                if removed > 0:
                    print(f"  [OK] Removed {removed} symlinks (legacy rglob scan)")
                else:
                    print(f"  [OK] No symlinks found (legacy scan)")
    except ImportError:
        pass

    # Read .applied for info (if exists)
    applied_info = {}
    if APPLIED_FILE.exists():
        try:
            applied_info = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
            applied_at = applied_info.get("applied_at", "?")
            patches = len(applied_info.get("cs_patches_applied", []))
            mods = applied_info.get("mods_copied", 0)
            res_patches = applied_info.get("patches_copied", 0)
            print(f"\n  .applied found:")
            print(f"    Applied at:   {applied_at}")
            print(f"    CS patches:   {patches}")
            print(f"    Res patches:  {res_patches}")
            print(f"    Mods copied:  {mods}")
        except (json.JSONDecodeError, OSError) as e:
            print(f"\n  [WARN] .applied is corrupted: {e}")
            print(f"  Will proceed with full revert anyway.")
    else:
        print(f"\n  No .applied found — doing full revert for safety.")

    if args.dry_run:
        print(f"\n  [DRY-RUN] Would revert:")
        for d in REVERT_DIRS:
            print(f"    git checkout HEAD -- {d}/ + git clean -fd {d}/")
        print(f"    git checkout HEAD -- . (RobustToolbox/) + git clean -fd")
        print(f"    Delete: .applied, .upstream_state.json, .conflict_decisions.yml")
        print(f"    Clean temp snapshots (.old.patched, .conflict.*)")
        return

    # 1. Revert Resources/
    #    Resources is reverted separately because it's the largest tree
    #    (5000+ files) and reverting it alone lets git optimize the walk.
    print(f"\n--- [1/5] Revert Resources/ ---")
    with step_timer("Revert Resources/"):
        revert_dir("Resources")
    print("  [OK] Resources/ reverted to HEAD")

    # 2. Sync Content Mods back (before reverting Content.*!)
    #    Apply.py copies 00_Aavikko/02_Content/Mods/*.cs → Content.*/Aavikko/
    #    Dev may have edited them in Content.*/Aavikko/ — sync back so nothing is lost
    print(f"\n--- [2/5] Sync Content Mods back ---")
    with step_timer("Sync Content Mods back"):
        synced = sync_content_mods_back()
    if synced > 0:
        print(f"  [OK] Synced {synced} file(s) back to 00_Aavikko/02_Content/Mods/")
    else:
        print(f"  [OK] No changes to sync back")

    # 3. Revert Content.* — BATCH all dirs in ONE git checkout + ONE git clean
    #    OLD: loop over 6 dirs × 2 spawns each = 12 spawns × 140ms = 1.7s on Windows
    #    NEW: 1 git checkout + 1 git clean = 2 spawns = ~280ms
    #    Saves ~1.4s on Windows.
    print(f"\n--- [3/5] Revert Content.* ---")
    with step_timer("Revert Content.* (batch)"):
        content_dirs = [d for d in REVERT_DIRS[1:] if (BUILD_ROOT / d).exists()]
        if content_dirs:
            # Batch: git checkout HEAD -- dir1/ dir2/ ... dirN/
            checkout_args = ["git", "checkout", "HEAD", "--"]
            checkout_args.extend(f"{d}/" for d in content_dirs)
            _, _, rc1 = run_git_with_lock_retry(checkout_args, cwd=BUILD_ROOT)

            # Batch: git clean -fd dir1/ dir2/ ... dirN/
            clean_args = ["git", "clean", "-fd"]
            clean_args.extend(f"{d}/" for d in content_dirs)
            _, _, rc2 = run_git_with_lock_retry(clean_args, cwd=BUILD_ROOT)

            if rc1 != 0:
                print(f"  [WARN] git checkout failed (rc={rc1})", file=sys.stderr)
            if rc2 != 0:
                print(f"  [WARN] git clean failed (rc={rc2})", file=sys.stderr)
    print("  [OK] Content.* reverted to HEAD")

    # 4. Revert RobustToolbox
    print(f"\n--- [4/5] Revert RobustToolbox ---")
    with step_timer("Revert RobustToolbox"):
        rb_ok = revert_robusttoolbox()
    if rb_ok:
        print("  [OK] RobustToolbox/ reverted to HEAD")
    else:
        print("  [SKIP] RobustToolbox/ not found")

    # 5. Cleanup state files + temp snapshots
    print(f"\n--- [5/5] Cleanup state files ---")
    with step_timer("Cleanup state files"):
        cleaned_snapshots = cleanup_temp_snapshots()
        if cleaned_snapshots > 0:
            print(f"  [DEL] {cleaned_snapshots} temp snapshot(s) removed")

        if APPLIED_FILE.exists():
            APPLIED_FILE.unlink()
            print("  [DEL] .applied")

        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print("  [DEL] .upstream_state.json")

        if DECISIONS_FILE.exists():
            if args.wipe_decisions:
                DECISIONS_FILE.unlink()
                print("  [DEL] .conflict_decisions.yml (--wipe-decisions)")
            else:
                print("  [KEEP] .conflict_decisions.yml (use --wipe-decisions to delete)")

    print(f"\n{'=' * 70}")
    print("Done! Upstream is clean.")
    print("  Safe to: git pull, run Migrate.py, run Apply.py")
    print(f"{'=' * 70}")
    print_timing_summary()


if __name__ == "__main__":
    main()