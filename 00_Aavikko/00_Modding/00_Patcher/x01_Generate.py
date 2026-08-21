#!/usr/bin/env python3
"""
Generate.py — capture git diff from upstream .cs file, save as .cs.patch.

Workflow:
  1. Developer edits an upstream .cs file (e.g. Content.Shared/Botany/Systems/PlantSystem.cs)
     in IDE — normal editing, IDE sees real project, type checking works.
  2. Developer runs: python3 x01_Generate.py Content.Shared/Botany/Systems/PlantSystem.cs
  3. Generate.py:
     a. Verifies the file exists and is tracked by git
     b. Runs `git diff <file>` to capture changes
     c. If diff is empty: "No changes to capture" → exit
     d. Saves diff to 00_Aavikko/02_Content/Patches/<mirror_path>.cs.patch
     e. Optionally runs `git checkout -- <file>` to restore upstream (with --restore flag)
     f. Prints summary

Usage:
  python3 x01_Generate.py <path/to/file.cs>
  python3 x01_Generate.py <path/to/file.cs> --restore   # restore upstream after capturing
  python3 x01_x01_Generate.py --all                          # capture all modified .cs files
  python3 x01_x01_Generate.py --list                         # list modified .cs files

The .patch file mirrors the upstream path:
  Upstream:   Content.Shared/Botany/Systems/PlantSystem.cs
  Patch:      00_Aavikko/02_Content/Patches/Content.Shared/Botany/Systems/PlantSystem.cs.patch

For .csproj patches (999-csproj-include-aavikko.cs.patch), use --csproj flag:
  python3 x01_Generate.py Content.Server/Content.Server.csproj --csproj
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

# Force UTF-8 for stdout/stderr — Windows default is cp1251 which can't encode
# Unicode characters like →, —, ✓ used in print() statements.
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        pass

from l01_ui import (
    header, section, divider, kv, ok, info, warn, error, fatal,
    skip, hint, tag, bullet, progress_iter, summary_table,
    success_banner, fail_banner, dim, bold,
    green, yellow, red, cyan, magenta,
)


SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_ROOT = SCRIPT_DIR.parent.parent.parent
PATCHES_DIR = BUILD_ROOT / "00_Aavikko/02_Content" / "Patches"
ROBUST_PATCHES_DIR = BUILD_ROOT / "00_Aavikko/03_RobustToolbox" / "Patches"
ROBUST_DIR = BUILD_ROOT / "RobustToolbox"

# Resources/ overlay: files are COPIED directly (no .patch diff generation)
# Apply.py copy_tree() handles them: Patches/ → overwrite upstream, Mods/ → new file.
RESOURCES_OVERLAY_DIR = BUILD_ROOT / "00_Aavikko/01_Resources"
RESOURCES_PATCHES_DIR = RESOURCES_OVERLAY_DIR / "Patches"
RESOURCES_MODS_DIR = RESOURCES_OVERLAY_DIR / "Mods"


# ── Path-type detection ────────────────────────────────────────────────────
# Determine which overlay a file belongs to based on its path prefix.
# Used to auto-route Generate.py to the correct workflow:
#   - Resources/  → copy file to 00_Aavikko/01_Resources/Patches/ or Mods/ (no diff)
#   - RobustToolbox/ (or Robust.*) → .cs.patch in 00_Aavikko/03_RobustToolbox/Patches/
#   - Content.*   → .cs.patch in 00_Aavikko/02_Content/Patches/


def detect_path_type(filepath: str) -> str:
    """Auto-detect which overlay a file belongs to.

    Returns one of: 'resources', 'robust', 'content'
    - 'resources'  → file is under Resources/ (YAML, audio, textures, etc.)
    - 'robust'     → file is under RobustToolbox/ or starts with Robust.*
    - 'content'    → file is under Content.* (default fallback)

    Path matching is case-insensitive on Windows but case-sensitive on Linux.
    We normalize to forward slashes first.
    """
    # Normalize: strip ./, convert backslashes to forward slashes
    norm = filepath.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]

    # RobustToolbox submodule: path is like "RobustToolbox/Robust.Shared/..."
    # OR it might start with Robust.* directly if --robust flag was passed
    # (running inside the submodule).
    if norm.startswith("RobustToolbox/") or norm.startswith("Robust.Shared/") or \
       norm.startswith("Robust.Client/") or norm.startswith("Robust.Server/"):
        return "robust"

    # Resources/ tree (everything under Resources/, regardless of extension)
    if norm.startswith("Resources/"):
        return "resources"

    # Default: Content.* files (Content.Server/, Content.Client/, etc.)
    return "content"


def is_git_tracked(filepath: str, cwd: Path | None = None) -> bool:
    """Check if a file is tracked by git AND exists in HEAD (committed upstream).

    Uses `git cat-file -e HEAD:<path>` which only returns 0 if the file exists
    in the HEAD commit. This is more strict than `git ls-files --error-unmatch`,
    which returns 0 for staged-but-not-committed files (e.g. new files just added
    via `git add` but not yet committed).

    For our purposes (Generate.py for Aavikko overlay), we want to know if the
    file exists in upstream HEAD — i.e. whether it's a real upstream file (Patches/)
    or a brand new Aavikko file (Mods/).
    """
    if cwd is None:
        cwd = BUILD_ROOT
    # git cat-file -e HEAD:<path> returns 0 if exists in HEAD, non-zero otherwise
    # Use argv list (no shell) for cross-platform compat
    result = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{filepath}"],
        cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace"
    )
    return result.returncode == 0


def run(cmd, cwd: Path | None = None, check: bool = True) -> tuple[str, str, int]:
    """Run a command. Accepts either a string (shell-style) OR an argv list.

    Cross-platform: never uses shell=True. If `cmd` is a string, it's split
    via shlex.split (which can BREAK on Windows paths with backslashes if
    they're not properly quoted). For safety, prefer passing an argv list.

    The argv list form is 100% cross-platform — no shell, no quoting,
    no backslash interpretation.
    """
    if isinstance(cmd, str):
        try:
            argv = shlex.split(cmd)
        except ValueError:
            # Fallback for edge cases (unbalanced quotes) — use shell
            result = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", errors="replace"
            )
        else:
            if not argv:
                return "", "", 0
            result = subprocess.run(
                argv, cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", errors="replace"
            )
    else:
        argv = list(cmd)
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )
    if check and result.returncode != 0:
        error(f"Command failed: {cmd}")
        hint(f"stderr: {result.stderr}")
        sys.exit(1)
    # NOTE: .strip() on stdout/stderr is OK for most commands (git rev-parse,
    # git status, etc.) but BREAKS git diff output — it removes trailing empty
    # context lines that are part of the patch format. For git diff, use
    # get_diff() which preserves the raw output.
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def get_diff(filepath: str) -> str:
    """Get git diff for a file (compares working tree vs HEAD).
    
    CRITICAL: must NOT strip trailing whitespace from output — git diff's patch
    format includes trailing empty context lines that are part of the hunk.
    Stripping them produces a patch with mismatched hunk counts
    (@@ -X,Y +A,B @@ says Y lines, but only Y-1 are present) →
    "corrupt patch at line N" error in git apply.
    """
    result = subprocess.run(
        ["git", "diff", "--", filepath],
        cwd=BUILD_ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace"
    )
    # Preserve trailing newlines — only strip leading/trailing whitespace
    # that's NOT part of the diff content. git diff output ends with \n after
    # the last hunk line — we keep that.
    # We do NOT strip here. Any trailing whitespace IS part of the patch.
    return result.stdout


def get_patch_dest(filepath: str, is_csproj: bool = False, is_robust: bool = False) -> Path:
    """Calculate patch destination path.

    For regular .cs files (Content.*): mirror upstream path
      Content.Shared/Botany/Systems/PlantSystem.cs
      → 00_Aavikko/02_Content/Patches/Content.Shared/Botany/Systems/PlantSystem.cs.patch

    For RobustToolbox .cs files: mirror path under 00_Aavikko/03_RobustToolbox/Patches/
      Robust.Shared/ProgramShared.cs
      → 00_Aavikko/03_RobustToolbox/Patches/Robust.Shared/ProgramShared.cs.patch

    For .csproj files: use 999-csproj-include-aavikko.cs.patch name
      Content.Server/Content.Server.csproj
      → 00_Aavikko/02_Content/Patches/Content.Server/999-csproj-include-aavikko.cs.patch
    """
    p = Path(filepath)
    base_dir = ROBUST_PATCHES_DIR if is_robust else PATCHES_DIR
    if is_csproj:
        # csproj patches go to <Project>/999-csproj-include-aavikko.cs.patch
        project = p.parent.name  # Content.Server, Content.Client, etc.
        return base_dir / project / "999-csproj-include-aavikko.cs.patch"
    else:
        # Regular .cs: mirror path + .patch extension
        return base_dir / f"{filepath}.patch"


def get_resources_dest(filepath: str) -> tuple[Path, str]:
    """For Resources/ files: calculate destination based on git-tracked status.

    Unlike Content.* files (which generate a .cs.patch diff), Resources/ files
    are COPIED DIRECTLY to the overlay folder. Apply.py's copy_tree() will later
    overwrite upstream (if in Patches/) or add as new file (if in Mods/).

    Returns:
      (dest_path, location) where location is 'Patches' or 'Mods'

    - Tracked by git (existing upstream file modified):
        Resources/Audio/foo.ogg → 00_Aavikko/01_Resources/Patches/Audio/foo.ogg
    - NOT tracked (new Aavikko file):
        Resources/Audio/foo.ogg → 00_Aavikko/01_Resources/Mods/Audio/foo.ogg
    """
    # Strip "Resources/" prefix for mirror path
    norm = filepath.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    if norm.startswith("Resources/"):
        rel = norm[len("Resources/"):]
    else:
        rel = norm

    if is_git_tracked(filepath):
        # Existing upstream file → Patches/ (overwrite on Apply)
        return (RESOURCES_PATCHES_DIR / rel, "Patches")
    else:
        # New file → Mods/ (added on Apply)
        return (RESOURCES_MODS_DIR / rel, "Mods")


def capture_resources_file(filepath: str, restore: bool = False) -> bool:
    """For Resources/ files: copy file to 00_Aavikko/01_Resources overlay (no diff).

    Resources files (YAML, audio, textures, etc.) don't need git-diff patch
    generation. Apply.py copies them directly via copy_tree():
      Patches/ → overwrite upstream Resources/ files
      Mods/    → add new Resources/ files

    Workflow:
      1. Detect if file is tracked by git (existing) or new
      2. Copy file to 00_Aavikko/01_Resources/Patches/<mirror> or Mods/<mirror>
      3. With --restore: revert upstream file (git checkout if tracked,
         delete if new) after copy

    Args:
      filepath: path relative to BUILD_ROOT (e.g. "Resources/Audio/foo.ogg")
      restore: if True, restore upstream after copy

    Returns True if saved successfully.
    """
    import shutil

    full_path = BUILD_ROOT / filepath
    if not full_path.exists():
        error(f"File not found: {filepath}")
        hint(f"BUILD_ROOT = {BUILD_ROOT}")
        return False

    # Calculate destination (Patches/ if tracked, Mods/ if new)
    dest, location = get_resources_dest(filepath)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Copy file (preserve metadata via copy2)
    try:
        shutil.copy2(full_path, dest)
    except OSError as e:
        error(f"Failed to copy file: {e}")
        return False

    # Verify
    if not dest.exists() or dest.stat().st_size != full_path.stat().st_size:
        error(f"Copy verification failed: {dest}")
        return False

    ok(f"Saved: {dest.relative_to(BUILD_ROOT)}  ({location}/)")
    kv("Source", filepath, indent=5)
    kv("Type", f"{'existing upstream file' if location == 'Patches' else 'new file'} → {location}/", indent=5)
    kv("Size", f"{full_path.stat().st_size} bytes", indent=5)

    # Optionally restore upstream
    if restore:
        if location == "Patches":
            # Tracked: git checkout to restore
            run(["git", "checkout", "--", filepath], cwd=BUILD_ROOT, check=False)
            ok(f"Restored upstream: {filepath}")
        else:
            # New file: just delete it (we already have a copy in Mods/)
            try:
                full_path.unlink()
                ok(f"Removed new file from upstream: {filepath}")
            except OSError as e:
                warn(f"Could not delete {filepath}: {e}")
                hint(f"File is still in Resources/ — delete manually if desired")

    return True


def capture_patch(filepath: str, restore: bool = False, is_csproj: bool = False,
                  keep_broken: bool = False, is_robust: bool = False) -> bool:
    """Capture git diff for a file and save as .patch. Returns True if saved.

    Verification strategy (CRITICAL):
    The patch is generated from working tree diff (changes vs HEAD). To verify
    the patch is valid, we apply --check against the HEAD version of the file
    (not the working tree). If we ran git apply --check against the working
    tree directly, the patch would ALWAYS fail — because the working tree
    already contains the changes.

    On verification failure, the patch is saved with `.broken` suffix (NOT deleted)
    so the developer can inspect/debug. Use --keep-broken to save with normal name.

    Set is_robust=True for RobustToolbox files (paths start with Robust.*)
    — patch is saved to 00_Aavikko/03_RobustToolbox/Patches/ and git apply runs
    with cwd=RobustToolbox/.
    """
    # Determine working directory and full path
    cwd = ROBUST_DIR if is_robust else BUILD_ROOT
    full_path = cwd / filepath

    # Verify file exists
    if not full_path.exists():
        error(f"File not found: {filepath}")
        hint(f"Check the path. {'RobustToolbox dir' if is_robust else 'BUILD_ROOT'} = {cwd}")
        return False

    # Verify file is tracked by git
    _, _, rc = run(["git", "ls-files", "--error-unmatch", filepath], cwd=cwd, check=False)
    if rc != 0:
        error(f"File is not tracked by git: {filepath}")
        hint("Generate.py only works on upstream files (tracked by git).")
        hint("For new Aavikko files, place them directly in 00_Aavikko/02_Content/Mods/ or 00_Aavikko/03_RobustToolbox/Mods/.")
        return False

    # Get diff (must use cwd-specific git, preserve raw output)
    diff_result = subprocess.run(
        ["git", "diff", "--", filepath],
        cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace"
    )
    diff = diff_result.stdout
    if not diff.strip():
        skip(f"No changes in {filepath} (working tree matches HEAD)")
        return False

    # Calculate destination
    dest = get_patch_dest(filepath, is_csproj=is_csproj, is_robust=is_robust)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Save patch
    dest.write_text(diff + "\n", encoding="utf-8")

    # CRITICAL: verify patch against CLEAN upstream (HEAD version), not working tree.
    # The working tree already has our changes — applying patch there always fails.
    # Strategy: save the developer's current content, checkout HEAD version of
    # the file, run git apply --check, then restore developer's content.
    # try/finally ensures developer's changes are NEVER lost.
    saved_ok = False
    original_content = full_path.read_bytes()
    try:
        # Checkout HEAD version (clean upstream)
        run(["git", "checkout", "HEAD", "--", filepath], cwd=cwd, check=False)

        # Verify patch against now-clean upstream
        # Use argv list + -c core.autocrlf=false for cross-platform CRLF compat
        _, verify_err, verify_rc = run(
            ["git", "-c", "core.autocrlf=false", "apply", "--check", str(dest)],
            cwd=cwd, check=False
        )

        if verify_rc == 0:
            ok(f"Patch saved + verified: {dest.relative_to(BUILD_ROOT)}")
            saved_ok = True
        else:
            # Verification failed — rename to .broken so dev can inspect
            broken_dest = dest.with_suffix(dest.suffix + ".broken")
            if dest.exists():
                if not broken_dest.exists():
                    dest.rename(broken_dest)
                else:
                    dest.unlink()
            error(f"Patch verification FAILED: {dest.relative_to(BUILD_ROOT)}")
            hint(f"git apply --check error: {verify_err[:300]}")
            if broken_dest.exists():
                hint(f"Patch saved as: {broken_dest.relative_to(BUILD_ROOT)}")
            hint("Common causes:")
            hint("  1. Hunk header counts don't match actual context/added/removed lines")
            hint("  2. Encoding issues (Russian comments mangled)")
            hint("  3. Patch was hand-edited and @@ -X,Y +A,B @@ wasn't updated")
            hint("Inspect .broken file or re-save with --keep-broken")
            saved_ok = False
    finally:
        # ALWAYS restore developer's original content (never lose changes!)
        full_path.write_bytes(original_content)

    if not saved_ok and not keep_broken:
        return False

    kv("Source", filepath, indent=5)
    kv("Diff size", f"{len(diff)} bytes", indent=5)

    # Optionally restore upstream (only if patch was saved OK)
    if restore and saved_ok:
        run(["git", "checkout", "--", filepath], cwd=BUILD_ROOT, check=False)
        ok(f"Restored upstream: {filepath}")
    
    return True


def list_modified_cs(is_robust: bool = False) -> list[str]:
    """List all modified .cs files (git status)."""
    cwd = ROBUST_DIR if is_robust else BUILD_ROOT
    stdout, _, _ = run("git status --porcelain", cwd=cwd, check=False)
    modified = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = parts[1].strip().split(" -> ")[-1].strip('"')
        # Only .cs files, only modified (M) or added (A) — not deleted
        if path.endswith(".cs") and status[0] in ("M", "A"):
            # Skip Aavikko.* paths
            if path.startswith("Aavikko."):
                continue
            modified.append(path)
    return modified


def list_modified_all() -> list[str]:
    """List all modified files (not just .cs) — includes Resources/, Content.*, RobustToolbox/.

    Used by --all flag to capture everything modified since last commit:
      - Resources/ files → copied to 00_Aavikko/01_Resources/Patches/ or Mods/
      - Content.* .cs/.xaml files → .cs.patch in 00_Aavikko/02_Content/Patches/
      - RobustToolbox files → .cs.patch in 00_Aavikko/03_RobustToolbox/Patches/

    Skips:
      - Aavikko.* paths (they ARE the overlay)
      - Patcher/ files (scripts, .applied state)
      - .csproj files (handled separately via --csproj flag)
      - Deleted files (D status) — we can't generate patch for a missing file
    """
    stdout, _, _ = run("git status --porcelain", cwd=BUILD_ROOT, check=False)
    modified = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = parts[1].strip().split(" -> ")[-1].strip('"')
        # Only modified (M) or added (A) — not deleted (D)
        if status[0] not in ("M", "A"):
            continue
        # Skip overlay paths (we don't generate patches for our own overlay)
        if path.startswith("Aavikko."):
            continue
        # Skip ALL 00_Aavikko/ paths (overlay, scripts, VSCode, etc.)
        if path.startswith("00_Aavikko/"):
            continue
        # Skip .csproj (handled separately via --csproj flag)
        if path.endswith(".csproj"):
            continue
        modified.append(path)

    # Also scan RobustToolbox submodule (separate git repo)
    if ROBUST_DIR.exists():
        stdout_rb, _, _ = run("git status --porcelain", cwd=ROBUST_DIR, check=False)
        for line in stdout_rb.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            status = parts[0].strip()
            path = parts[1].strip().split(" -> ")[-1].strip('"')
            if status[0] not in ("M", "A"):
                continue
            if path.endswith(".csproj"):
                continue
            # Prefix with RobustToolbox/ so detect_path_type routes correctly
            modified.append(f"RobustToolbox/{path}")

    return modified


def main():
    parser = argparse.ArgumentParser(
        description="Generate overlay files from upstream changes.\n"
                   "  - Resources/ files: copied to 00_Aavikko/01_Resources/Patches/ (tracked) or Mods/ (new)\n"
                   "  - Content.* .cs/.xaml files: .cs.patch in 00_Aavikko/02_Content/Patches/\n"
                   "  - RobustToolbox files: .cs.patch in 00_Aavikko/03_RobustToolbox/Patches/\n"
                   "Path type is auto-detected from the filepath prefix.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("filepath", nargs="?", help="Path to file (relative to build root)")
    parser.add_argument("--restore", action="store_true",
                        help="Restore upstream file after capturing (git checkout for tracked, delete for new)")
    parser.add_argument("--all", action="store_true",
                        help="Capture ALL modified files (auto-routes by path type)")
    parser.add_argument("--list", action="store_true",
                        help="List modified files (all types: Resources/, Content.*, RobustToolbox/)")
    parser.add_argument("--csproj", action="store_true",
                        help="Treat filepath as .csproj (saves as 999-csproj-include-aavikko.cs.patch)")
    parser.add_argument("--robust", action="store_true",
                        help="Force RobustToolbox mode (auto-detected if path starts with RobustToolbox/)")
    parser.add_argument("--keep-broken", action="store_true",
                        help="Save patch even if git apply --check fails (debugging only)")
    args = parser.parse_args()
    
    header("Aavikko Overlay Generator", "capture changes → Patches/Mods overlay")
    
    if args.list:
        modified = list_modified_all()
        if not modified:
            info("No modified files found.")
        else:
            print()
            print(f"  {bold(f'Modified files ({len(modified)}):')}")
            # Group by type for readability
            by_type = {"resources": [], "robust": [], "content": []}
            for f in modified:
                by_type[detect_path_type(f)].append(f)
            for type_name, label in [("resources", "Resources/ (copy)"),
                                     ("content", "Content.* (.cs.patch)"),
                                     ("robust", "RobustToolbox/ (.cs.patch)")]:
                files = by_type[type_name]
                if not files:
                    continue
                print(f"\n  {cyan(label)} ({len(files)}):")
                for f in files[:20]:
                    print(f"    {f}")
                if len(files) > 20:
                    print(f"    ... and {len(files) - 20} more")
            print()
            hint(f"Run: python3 x01_x01_Generate.py --all")
            hint(f"Or capture one: python3 x01_Generate.py <path>")
        return
    
    if args.all:
        modified = list_modified_all()
        if not modified:
            info("No modified files to capture.")
            return
        section("all", None, f"Capturing {len(modified)} file(s)")
        saved = 0
        failed = 0
        for f in modified:
            print()
            path_type = detect_path_type(f)
            if path_type == "resources":
                if capture_resources_file(f, restore=args.restore):
                    saved += 1
                else:
                    failed += 1
            elif path_type == "robust":
                # Strip "RobustToolbox/" prefix for capture_patch (it runs inside ROBUST_DIR)
                robust_path = f[len("RobustToolbox/"):] if f.startswith("RobustToolbox/") else f
                if capture_patch(robust_path, restore=args.restore, keep_broken=args.keep_broken,
                                 is_robust=True):
                    saved += 1
                else:
                    failed += 1
            else:
                if capture_patch(f, restore=args.restore, is_csproj=args.csproj,
                                 keep_broken=args.keep_broken, is_robust=False):
                    saved += 1
                else:
                    failed += 1
        if failed > 0:
            fail_banner(
                f"Saved {saved}/{len(modified)} — {failed} FAILED",
                hints=[
                    "Files that failed verification were NOT saved.",
                    "Fix the source file or use --keep-broken for debugging (.cs only).",
                ],
            )
            sys.exit(1)
        success_banner(
            f"Saved {saved}/{len(modified)} file(s)",
            next_step=("All upstream files restored to HEAD." if args.restore else None),
        )

        # ── Post-step: regenerate showcase map ──
        try:
            import generate_showcase_map
            saved_argv = sys.argv[:]
            sys.argv = [sys.argv[0]]
            generate_showcase_map.main()
            sys.argv = saved_argv
        except Exception as e:
            print(f"  [WARN] Showcase map generation failed: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
        return
    
    if not args.filepath:
        parser.error("filepath required (or use --all / --list)")
    
    filepath = args.filepath
    # Normalize path (remove leading ./, normalize backslashes)
    filepath = filepath.replace("\\", "/")
    if filepath.startswith("./"):
        filepath = filepath[2:]
    
    # Auto-detect path type (unless --robust overrides)
    path_type = detect_path_type(filepath)
    # --robust flag overrides detection (for paths inside the submodule without prefix)
    if args.robust and path_type != "robust":
        path_type = "robust"
    
    section("single", None, f"Capturing: {filepath}" +
            (f"  ({path_type}/)" if path_type != "content" else ""))
    
    if path_type == "resources":
        # Resources/ file → direct copy (no diff)
        success = capture_resources_file(filepath, restore=args.restore)
        if success:
            if args.restore:
                success_banner("Done!", next_step="Upstream restored. File saved to overlay.")
            else:
                success_banner(
                    "Done!",
                    next_step=f"File copied to overlay. Upstream still has changes.\n"
                              f"  Run `git checkout -- {filepath}` to restore when done.",
                )
        else:
            sys.exit(1)
    elif path_type == "robust":
        # RobustToolbox .cs file → .cs.patch
        # Strip "RobustToolbox/" prefix if present (capture_patch expects path relative to ROBUST_DIR)
        robust_path = filepath[len("RobustToolbox/"):] if filepath.startswith("RobustToolbox/") else filepath
        success = capture_patch(robust_path, restore=args.restore, is_csproj=args.csproj,
                                keep_broken=args.keep_broken, is_robust=True)
        if success:
            if args.restore:
                success_banner("Done!", next_step="Upstream file restored. Patch saved separately.")
            else:
                success_banner(
                    "Done!",
                    next_step=f"Patch saved + verified. Upstream file still has changes.\n"
                              f"  Run `git checkout -- {filepath}` to restore when done.",
                )
        else:
            sys.exit(1)
    else:
        # Content.* file → .cs.patch
        success = capture_patch(filepath, restore=args.restore, is_csproj=args.csproj,
                               keep_broken=args.keep_broken, is_robust=False)
        if success:
            if args.restore:
                success_banner("Done!", next_step="Upstream file restored. Patch saved separately.")
            else:
                success_banner(
                    "Done!",
                    next_step=f"Patch saved + verified. Upstream file still has changes.\n"
                              f"  Run `git checkout -- {filepath}` to restore when done.",
                )
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()