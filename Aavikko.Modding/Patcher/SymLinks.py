#!/usr/bin/env python3
"""
SymLinks.py — create/remove navigation symlinks for Aavikko overlay.

Two types of symlinks:

1. **@Mods/@Patches** — cross-navigation between Mods/ and Patches/ folders.
   Click @Patches in Mods/Audio/ → jump to Patches/Audio/.

2. **@Path** — pointer from overlay folder to the corresponding folder in
   the build tree (Resources/, Content.*/, RobustToolbox/).
   Click @Path in Aavikko.Resources/Mods/Audio/ → jump to Resources/Audio/.
   Shows what's currently in the build (Aavikko files after Apply, upstream
   files after Clear).

3. **@patched** — symlink next to .cs.patch files, points to the actual
   patched .cs file in the build tree (after Apply).
   Created by Apply.py, removed by Clear.py.

Layout (after `python3 SymLinks.py create`):

  Aavikko.Resources/
  ├── Mods/
  │   ├── Audio/
  │   │   ├── @Patches/  ← symlink → ../../Patches/Audio/
  │   │   └── @Path/     ← symlink → ../../../Resources/Audio/
  │   └── ...
  ├── Patches/
  │   ├── Audio/
  │   │   ├── @Mods/     ← symlink → ../../Mods/Audio/
  │   │   └── @Path/     ← symlink → ../../../Resources/Audio/
  │   └── ...

  Aavikko.Content/
  ├── Patches/
  │   └── Content.Server/
  │       └── Foo/
  │           ├── Bar.cs.patch
  │           ├── Bar.cs@patched  ← symlink → ../../../../Content.Server/Foo/Bar.cs (after Apply)
  │           └── @Path/          ← symlink → ../../../../Content.Server/Foo/
  └── ...

Cross-platform:
  - Linux: os.symlink (native)
  - Windows: os.symlink (requires admin or developer mode on Win10+)
             Falls back to creating a .txt file with the path
  - macOS: os.symlink (native)

Usage:
  python3 SymLinks.py create   # create @Mods/@Patches + @Path symlinks
  python3 SymLinks.py remove    # remove all nav symlinks (NOT @patched — those are Apply/Clear managed)
  python3 SymLinks.py list      # show existing symlinks

@patched symlinks are managed by Apply.py (creates) and Clear.py (removes).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 for stdout/stderr — Windows default is cp1251 which can't encode
# Unicode characters like → used in print() statements.
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_ROOT = SCRIPT_DIR.parent.parent

# State file: tracks every symlink we create, so removal is O(N) unlink calls
# instead of O(filesystem tree) rglob walks. On HDD with 5000+ overlay files,
# this is the difference between 0.1s and 50s.
# Paths stored relative to BUILD_ROOT (portable across sandbox/host mount points).
SYMLINK_STATE_FILE = SCRIPT_DIR / ".symlinks.json"

# All overlay pairs to process.
# Each tuple: (overlay_root, label, build_target_dir)
#   - overlay_root: Aavikko.* folder
#   - label: display name
#   - build_target_dir: where in the build tree these files end up after Apply
OVERLAY_PAIRS = [
    (BUILD_ROOT / "Aavikko.Resources", "Resources", BUILD_ROOT / "Resources"),
    (BUILD_ROOT / "Aavikko.Content", "Content", BUILD_ROOT),  # Content.* paths already include project name
    (BUILD_ROOT / "Aavikko.RobustToolbox", "RobustToolbox", BUILD_ROOT / "RobustToolbox"),
]

# Names of the navigation folders (sorted to top via @ prefix)
MODS_LINK_NAME = "@Mods"
PATCHES_LINK_NAME = "@Patches"
PATH_LINK_NAME = "@Path"
PATCHED_LINK_SUFFIX = "@patched"  # appended to filename: Bar.cs@patched


def is_nav_symlink(path: Path) -> bool:
    """Check if a path is one of our navigation symlinks (@Mods/@Patches/@Path)."""
    return path.name in (MODS_LINK_NAME, PATCHES_LINK_NAME, PATH_LINK_NAME) and path.is_symlink()


# ── State file: fast-path removal ─────────────────────────────────────────


def _load_symlink_state() -> list[str]:
    """Load list of created symlinks from .symlinks.json.
    Returns list of paths relative to BUILD_ROOT. Returns [] if missing/corrupt.
    """
    if not SYMLINK_STATE_FILE.exists():
        return []
    try:
        data = json.loads(SYMLINK_STATE_FILE.read_text(encoding="utf-8"))
        return list(data.get("symlinks", []))
    except (OSError, json.JSONDecodeError):
        return []


def _save_symlink_state(symlinks: list[str]) -> None:
    """Write list of created symlinks to .symlinks.json (atomic)."""
    data = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symlinks": symlinks,
    }
    tmp = SYMLINK_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, SYMLINK_STATE_FILE)
    except OSError:
        pass  # best-effort


def _clear_symlink_state() -> None:
    """Delete the state file after all symlinks removed."""
    try:
        SYMLINK_STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def reset_symlink_state() -> None:
    """Public helper: clear state file before creating fresh symlinks.

    Apply.py calls this right before `create_patched_links()` so the state
    file contains ONLY the symlinks created in this run (not stale entries
    from removed patches).
    """
    _clear_symlink_state()


def symlinks_likely_exist() -> bool:
    """Quick heuristic: are there any symlinks we created on disk?

    Used by Apply.py / Clear.py to decide whether to even attempt the
    removal step. If `.applied` exists OR `.symlinks.json` exists, we
    might have symlinks — try removal. If neither, skip entirely (saves
    the slow rglob fallback when called right after a fresh Clear.py).

    Note: this is a heuristic, not a guarantee. The only 100% reliable
    check is `os.walk()` + `is_symlink()` on every file, which is exactly
    what we're trying to avoid.
    """
    # Local import to avoid circular dependency at module load time
    applied_marker = SCRIPT_DIR / ".applied"
    return SYMLINK_STATE_FILE.exists() or applied_marker.exists()


def _record_symlink(link_path: Path) -> None:
    """Record a created symlink in the state file (append).
    Path stored relative to BUILD_ROOT for portability.

    IMPORTANT: do NOT use link_path.resolve() — that follows the symlink to its
    TARGET, which would record the wrong path. Use .absolute() instead which just
    makes the path absolute without following symlinks.
    """
    try:
        # absolute() doesn't resolve symlinks (Path.absolute() is Py 3.11+,
        # fallback to os.path.abspath for older versions)
        if hasattr(link_path, 'absolute'):
            link_abs = link_path.absolute()
        else:
            link_abs = Path(os.path.abspath(str(link_path)))
        if hasattr(BUILD_ROOT, 'absolute'):
            build_abs = BUILD_ROOT.absolute()
        else:
            build_abs = Path(os.path.abspath(str(BUILD_ROOT)))
        try:
            rel = str(link_abs.relative_to(build_abs))
        except ValueError:
            # Link outside BUILD_ROOT — store absolute as fallback
            rel = str(link_abs)
    except OSError:
        rel = str(link_path)
    existing = _load_symlink_state()
    if rel not in existing:
        existing.append(rel)
        _save_symlink_state(existing)


def remove_all_tracked_symlinks() -> int:
    """Fast-path: read .symlinks.json, unlink each entry, KEEP the state file.

    This is the O(N) fast path — N = number of symlinks we created (typically 32).
    Avoids the O(filesystem tree) rglob walk which on HDD takes 50+ seconds
    for trees with 5000+ files.

    IMPORTANT: As of v0.3.1, this function does NOT clear the state file
    after removal. Previously it did, which meant the *next* Apply.py run
    (after a Clear.py) had no state file → fell back to slow rglob scan.
    Now the state file is kept as a "we tried to remove these" record;
    Apply.py refreshes it (clear + recreate) before creating new symlinks.

    Returns:
      >=0: number of symlinks removed via fast path (0 is normal after Clear.py)
      -1 : state file missing — caller should fall back to slow rglob method
    """
    if not SYMLINK_STATE_FILE.exists():
        return -1

    entries = _load_symlink_state()
    if not entries:
        # State file exists but is empty (shouldn't normally happen —
        # _save_symlink_state always writes a non-empty list). Clear it
        # so we don't keep reading a corrupt file.
        _clear_symlink_state()
        return 0

    removed = 0
    # Use absolute() (not resolve()) so we keep the symlink path itself,
    # not the target. resolve() would follow the link and we'd unlink the wrong file.
    if hasattr(BUILD_ROOT, 'absolute'):
        build_root = BUILD_ROOT.absolute()
    else:
        build_root = Path(os.path.abspath(str(BUILD_ROOT)))

    for rel in entries:
        # build_root / rel preserves relative structure; if rel is absolute,
        # Path's / operator returns the absolute path
        link_path = build_root / rel
        try:
            # is_symlink() checks the link itself; exists() follows the link
            # Use OR so we catch both live and dead symlinks
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
                removed += 1
        except OSError:
            pass  # already gone, permission issue, etc.

    # NOTE: deliberately NOT calling _clear_symlink_state() here.
    # The state file is kept so the next Apply.py can read it (fast path)
    # even when all entries are already removed (post-Clear scenario).
    # Apply.py calls _clear_symlink_state() explicitly before creating
    # new symlinks, so the file never grows unbounded.
    return removed


def is_patched_symlink(path: Path) -> bool:
    """Check if a path is a @patched symlink (for .cs.patch files)."""
    return path.name.endswith(PATCHED_LINK_SUFFIX) and path.is_symlink()


def find_subdirs(parent: Path) -> list[Path]:
    """Find all subdirectories in a parent folder (excluding nav symlinks)."""
    if not parent.exists():
        return []
    result = []
    for entry in sorted(parent.iterdir()):
        if entry.is_dir() and not is_nav_symlink(entry):
            result.append(entry)
    return result


def create_symlink_safe(link_path: Path, target: Path) -> bool:
    """Create a symlink, handling Windows limitations.

    Uses RELATIVE paths for the symlink target so symlinks work across
    different machines (sandbox /workspace/... vs host /media/Crucible/...).
    Absolute paths would break when the build root is at a different location.

    Returns True if symlink created (or already exists pointing to correct target).
    Returns False if symlink creation failed (caller may create text fallback).
    """
    # Already exists?
    if link_path.is_symlink():
        try:
            # Compare resolved targets (works for both relative and absolute symlinks)
            if link_path.resolve() == target.resolve():
                return True  # Already correct
        except OSError:
            pass
        # Wrong target — remove and recreate
        try:
            link_path.unlink()
        except OSError:
            return False

    # Make sure parent exists
    link_path.parent.mkdir(parents=True, exist_ok=True)

    # Make sure target exists (or symlink will be dead)
    if not target.exists():
        return False

    # Calculate RELATIVE path from link's parent to target
    # This makes symlinks portable across machines with different build root locations
    try:
        rel_target = os.path.relpath(target, link_path.parent)
    except ValueError:
        # On Windows, relpath fails across drives (C: vs D:)
        # Fall back to absolute path
        rel_target = str(target)

    try:
        os.symlink(rel_target, link_path, target_is_directory=True)
        # Record in state file for fast removal later (avoids 50s rglob walk on HDD)
        _record_symlink(link_path)
        return True
    except (OSError, NotImplementedError):
        # Windows without admin/developer mode, or platform without symlinks
        return False


def create_text_fallback(link_path: Path, target: Path) -> bool:
    """Create a text file with the path as fallback when symlinks don't work.

    User can open the .txt file to see the path, then navigate manually.
    Better than nothing on restricted Windows.
    """
    txt_path = link_path.with_suffix(".txt")
    try:
        txt_path.write_text(
            f"This is a navigation link to:\n{target}\n\n"
            f"Symlink creation failed (Windows without admin/developer mode).\n"
            f"Open this path manually in your file manager.\n",
            encoding="utf-8"
        )
        # Record fallback in state file too (so removal can clean it up)
        _record_symlink(txt_path)
        return True
    except OSError:
        return False


# ── @Path symlinks (overlay → build tree) ────────────────────────────────────


def has_files(path: Path, recursive: bool = False) -> bool:
    """Check if a directory contains any real files.

    Args:
      path: directory to check
      recursive: if True, search recursively (in subdirs too).
                 if False (default), only check direct files in this folder.

    Returns True if there's at least one real file (not symlink, not .gitkeep).
    Used to skip creating @Path/@Mods/@Patches in folders that only contain
    subdirectories (no actual files at that level).
    """
    if not path.is_dir():
        return False

    if not recursive:
        # Only check direct files in this folder (not subdirs)
        try:
            for entry in path.iterdir():
                if not entry.is_file():
                    continue
                if entry.is_symlink():
                    continue  # Skip @patched, .txt fallbacks
                if entry.name.startswith(".gitkeep"):
                    continue
                return True
        except OSError:
            pass
        return False
    else:
        # Recursive: search all subdirs (but don't follow symlinks)
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if not is_nav_symlink(Path(root) / d)]
            for fname in files:
                if fname.startswith(".gitkeep"):
                    continue
                fpath = Path(root) / fname
                if fpath.is_symlink():
                    continue
                return True
        return False


def create_path_links_for_overlay(overlay_root: Path, label: str,
                                  build_target_dir: Path) -> tuple[int, int]:
    """Create @Path symlinks from overlay folders to build tree.

    Creates @Path in:
      1. Mods/ and Patches/ root level (if they have direct files)
         → points to build_target_dir (Resources/, Content.*, etc.)
      2. Top-level subdirectories of Mods/ and Patches/ (if they have direct files)
         → points to corresponding subdir in build tree

    Only creates @Path in folders that ACTUALLY CONTAIN DIRECT FILES
    (not just subdirectories). Empty folders are skipped.

    Example:
      Aavikko.Resources/Patches/@Path → ../../Resources/  (has clientCommandPerms.yml)
      Aavikko.Resources/Mods/Audio/@Path → ../../../Resources/Audio/  (has .ogg files)
    """
    symlinks_created = 0
    fallbacks_created = 0

    for subdir_name in ("Mods", "Patches"):
        subdir = overlay_root / subdir_name
        if not subdir.exists():
            continue

        # 1. Create @Path at Mods/ or Patches/ root level (if has direct files)
        if has_files(subdir):
            if build_target_dir.exists():
                link_path = subdir / PATH_LINK_NAME
                if create_symlink_safe(link_path, build_target_dir):
                    symlinks_created += 1
                    print(f"  [OK] {label}/{subdir_name}/{PATH_LINK_NAME} → {build_target_dir.relative_to(BUILD_ROOT)}/")

        # 2. Create @Path in top-level subdirectories (depth 1)
        for child in sorted(subdir.iterdir()):
            if not child.is_dir():
                continue
            if is_nav_symlink(child):
                continue

            # Skip if overlay folder has no direct files
            if not has_files(child):
                continue

            # Calculate relative path from overlay root
            rel = child.relative_to(overlay_root / subdir_name)
            target = build_target_dir / rel

            # Only create @Path if target exists in build tree
            if not target.exists():
                continue

            link_path = child / PATH_LINK_NAME
            if create_symlink_safe(link_path, target):
                symlinks_created += 1
                display_rel = child.relative_to(overlay_root)
                print(f"  [OK] {label}/{display_rel}/{PATH_LINK_NAME} → {target.relative_to(BUILD_ROOT)}/")
            else:
                if create_text_fallback(link_path, target):
                    fallbacks_created += 1

    return (symlinks_created, fallbacks_created)


def remove_path_links_for_overlay(overlay_root: Path, label: str) -> int:
    """Remove all @Path symlinks from one overlay."""
    removed = 0
    for subdir_name in ("Mods", "Patches"):
        subdir = overlay_root / subdir_name
        if not subdir.exists():
            continue

        # Find all @Path symlinks (they could be at any depth)
        for child in sorted(subdir.rglob(PATH_LINK_NAME)):
            if child.is_symlink():
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    pass
        # Also remove .txt fallbacks
        for child in sorted(subdir.rglob(PATH_LINK_NAME + ".txt")):
            if child.exists():
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


# ── @patched symlinks (.cs.patch → patched .cs file in build) ────────────────


def create_patched_links(overlay_root: Path, label: str,
                         build_target_dir: Path) -> tuple[int, int]:
    """Create @patched symlinks next to .cs.patch files.

    For each .cs.patch file in Patches/, create a symlink:
      Foo.cs.patch  →  Foo.cs@patched  (symlink to build_target_dir/Foo.cs)

    Only creates if the target .cs file exists (i.e. after Apply).
    Called by Apply.py after successful patch application.
    """
    symlinks_created = 0
    fallbacks_created = 0
    patches_dir = overlay_root / "Patches"
    if not patches_dir.exists():
        return (0, 0)

    for patch_file in sorted(patches_dir.rglob("*.cs.patch")):
        # Calculate the upstream .cs file path
        rel = patch_file.relative_to(patches_dir)
        # Strip .patch suffix to get .cs path
        # IMPORTANT: check .xaml.cs.patch BEFORE .cs.patch (it also ends with .cs.patch)
        cs_rel = str(rel)
        if cs_rel.endswith(".xaml.cs.patch"):
            cs_rel = cs_rel[:-len(".patch")]  # → .xaml.cs
        elif cs_rel.endswith(".cs.patch"):
            cs_rel = cs_rel[:-len(".patch")]  # → .cs
        else:
            continue

        target_cs = build_target_dir / cs_rel
        if not target_cs.exists():
            continue  # Patch not applied yet, or file deleted

        # Symlink name: Foo.cs@patched (next to Foo.cs.patch)
        link_path = patch_file.with_name(target_cs.name + PATCHED_LINK_SUFFIX)
        if create_symlink_safe(link_path, target_cs):
            symlinks_created += 1
            display_rel = patch_file.relative_to(overlay_root)
            print(f"  [OK] {label}/{display_rel}{PATCHED_LINK_SUFFIX} → {target_cs.relative_to(BUILD_ROOT)}")
        else:
            if create_text_fallback(link_path, target_cs):
                fallbacks_created += 1

    # Also handle .xaml.patch (not .xaml.cs.patch — those are caught above)
    for patch_file in sorted(patches_dir.rglob("*.xaml.patch")):
        # Skip .xaml.cs.patch — already processed above
        if patch_file.name.endswith(".xaml.cs.patch"):
            continue
        rel = patch_file.relative_to(patches_dir)
        cs_rel = str(rel)
        if cs_rel.endswith(".xaml.patch"):
            cs_rel = cs_rel[:-len(".patch")]  # → .xaml
        else:
            continue

        target_cs = build_target_dir / cs_rel
        if not target_cs.exists():
            continue

        # Symlink name: Foo.cs@patched (next to Foo.cs.patch)
        link_path = patch_file.with_name(target_cs.name + PATCHED_LINK_SUFFIX)
        if create_symlink_safe(link_path, target_cs):
            symlinks_created += 1
            display_rel = patch_file.relative_to(overlay_root)
            print(f"  [OK] {label}/{display_rel}{PATCHED_LINK_SUFFIX} → {target_cs.relative_to(BUILD_ROOT)}")
        else:
            if create_text_fallback(link_path, target_cs):
                fallbacks_created += 1

    return (symlinks_created, fallbacks_created)


def remove_patched_links(overlay_root: Path, label: str) -> int:
    """Remove all @patched symlinks from one overlay.

    Called by Clear.py before reverting upstream (so symlinks don't dangle).
    """
    removed = 0
    for subdir_name in ("Mods", "Patches"):
        subdir = overlay_root / subdir_name
        if not subdir.exists():
            continue
        # Find all *<PATCHED_LINK_SUFFIX> files (symlinks or .txt fallbacks)
        for child in sorted(subdir.rglob(f"*{PATCHED_LINK_SUFFIX}")):
            if child.is_symlink() or child.exists():
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    pass
        # .txt fallbacks have .txt suffix appended
        for child in sorted(subdir.rglob(f"*{PATCHED_LINK_SUFFIX}.txt")):
            if child.exists():
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def create_nav_links_for_pair(overlay_root: Path, label: str) -> tuple[int, int]:
    """Create navigation symlinks for one overlay pair (Mods/Patches).

    For each subdirectory in Mods/ (e.g. Mods/Audio/), create:
      - Mods/Audio/@Patches/ → ../../Patches/Audio/ (if Patches/Audio/ exists)
      - Mods/Audio/@Patches-Parent/ → ../../Patches/ (always, for jumping to Patches root)

    Same in reverse for Patches/.

    Returns: (symlinks_created, fallbacks_created)
    """
    mods_dir = overlay_root / "Mods"
    patches_dir = overlay_root / "Patches"

    if not mods_dir.exists() and not patches_dir.exists():
        return (0, 0)

    symlinks_created = 0
    fallbacks_created = 0

    # Process Mods/ subdirs — add @Patches links (only if both have files)
    for mod_subdir in find_subdirs(mods_dir):
        # Skip if Mods/<subdir> has no files
        if not has_files(mod_subdir):
            continue
        rel = mod_subdir.relative_to(mods_dir)
        patches_target = patches_dir / rel

        # @Patches symlink inside Mods/<subdir>/
        link_path = mod_subdir / PATCHES_LINK_NAME
        if patches_target.exists() and has_files(patches_target):
            if create_symlink_safe(link_path, patches_target):
                symlinks_created += 1
                print(f"  [OK] {label}/Mods/{rel}/{PATCHES_LINK_NAME} → Patches/{rel}/")
            else:
                if create_text_fallback(link_path, patches_target):
                    fallbacks_created += 1
                    print(f"  [TXT] {label}/Mods/{rel}/{PATCHES_LINK_NAME}.txt (fallback)")
        # If Patches/<rel> doesn't exist or has no files, skip

    # Process Patches/ subdirs — add @Mods links (only if both have files)
    for patches_subdir in find_subdirs(patches_dir):
        # Skip if Patches/<subdir> has no files
        if not has_files(patches_subdir):
            continue
        rel = patches_subdir.relative_to(patches_dir)
        mods_target = mods_dir / rel

        link_path = patches_subdir / MODS_LINK_NAME
        if mods_target.exists() and has_files(mods_target):
            if create_symlink_safe(link_path, mods_target):
                symlinks_created += 1
                print(f"  [OK] {label}/Patches/{rel}/{MODS_LINK_NAME} → Mods/{rel}/")
            else:
                if create_text_fallback(link_path, mods_target):
                    fallbacks_created += 1
                    print(f"  [TXT] {label}/Patches/{rel}/{MODS_LINK_NAME}.txt (fallback)")

    # Also create top-level @Mods and @Patches at overlay root for quick jump
    if mods_dir.exists() and patches_dir.exists():
        # @Patches at overlay root → Patches/
        top_link = mods_dir / PATCHES_LINK_NAME
        if create_symlink_safe(top_link, patches_dir):
            symlinks_created += 1
            print(f"  [OK] {label}/Mods/{PATCHES_LINK_NAME} → Patches/ (root jump)")

        # @Mods at overlay root → Mods/
        top_link = patches_dir / MODS_LINK_NAME
        if create_symlink_safe(top_link, mods_dir):
            symlinks_created += 1
            print(f"  [OK] {label}/Patches/{MODS_LINK_NAME} → Mods/ (root jump)")

    return (symlinks_created, fallbacks_created)


def remove_nav_links_for_pair(overlay_root: Path, label: str) -> int:
    """Remove all @Mods/@Patches symlinks from one overlay pair."""
    removed = 0
    for subdir_name in ("Mods", "Patches"):
        subdir = overlay_root / subdir_name
        if not subdir.exists():
            continue

        # Remove top-level nav links
        for link_name in (MODS_LINK_NAME, PATCHES_LINK_NAME):
            link_path = subdir / link_name
            if link_path.is_symlink():
                try:
                    link_path.unlink()
                    removed += 1
                    print(f"  [DEL] {label}/{subdir_name}/{link_name}")
                except OSError:
                    pass
            # Also remove .txt fallback
            txt_path = link_path.with_suffix(".txt")
            if txt_path.exists():
                try:
                    txt_path.unlink()
                    removed += 1
                    print(f"  [DEL] {label}/{subdir_name}/{link_name}.txt")
                except OSError:
                    pass

        # Remove nav links inside subdirectories
        for child in find_subdirs(subdir):
            for link_name in (MODS_LINK_NAME, PATCHES_LINK_NAME):
                link_path = child / link_name
                if link_path.is_symlink():
                    try:
                        link_path.unlink()
                        removed += 1
                        rel = child.relative_to(overlay_root)
                        print(f"  [DEL] {label}/{rel}/{link_name}")
                    except OSError:
                        pass
                txt_path = link_path.with_suffix(".txt")
                if txt_path.exists():
                    try:
                        txt_path.unlink()
                        removed += 1
                    except OSError:
                        pass

    return removed


def list_nav_links_for_pair(overlay_root: Path, label: str) -> int:
    """List all existing nav symlinks in one overlay pair (@Mods/@Patches/@Path/@patched)."""
    found = 0
    for subdir_name in ("Mods", "Patches"):
        subdir = overlay_root / subdir_name
        if not subdir.exists():
            continue

        # Check top-level @Mods/@Patches
        for link_name in (MODS_LINK_NAME, PATCHES_LINK_NAME):
            link_path = subdir / link_name
            if link_path.is_symlink():
                target = link_path.resolve()
                print(f"  {label}/{subdir_name}/{link_name} → {target}")
                found += 1
            txt_path = link_path.with_suffix(".txt")
            if txt_path.exists():
                print(f"  {label}/{subdir_name}/{link_name}.txt (text fallback)")
                found += 1

        # Check subdirs recursively for @Mods/@Patches/@Path
        for child in sorted(subdir.rglob("*")):
            if not child.exists():
                continue
            # @Path symlinks
            if child.name == PATH_LINK_NAME and child.is_symlink():
                target = child.resolve()
                rel = child.parent.relative_to(overlay_root)
                print(f"  {label}/{rel}/{PATH_LINK_NAME} → {target}")
                found += 1
            # @Mods/@Patches symlinks (only at non-top level)
            elif child.name in (MODS_LINK_NAME, PATCHES_LINK_NAME) and child.is_symlink():
                target = child.resolve()
                rel = child.parent.relative_to(overlay_root)
                print(f"  {label}/{rel}/{child.name} → {target}")
                found += 1
            # @patched symlinks
            elif is_patched_symlink(child):
                target = child.resolve()
                rel = child.parent.relative_to(overlay_root)
                print(f"  {label}/{rel}/{child.name} → {target}")
                found += 1
            # .txt fallbacks
            elif child.name.endswith(".txt") and (
                child.name.startswith(MODS_LINK_NAME) or
                child.name.startswith(PATCHES_LINK_NAME) or
                child.name.startswith(PATH_LINK_NAME) or
                child.name.endswith(PATCHED_LINK_SUFFIX + ".txt")
            ):
                rel = child.parent.relative_to(overlay_root)
                print(f"  {label}/{rel}/{child.name} (text fallback)")
                found += 1
    return found


def main():
    parser = argparse.ArgumentParser(
        description="Create/remove navigation symlinks for Aavikko overlay"
    )
    parser.add_argument("action",
                        choices=["create", "remove", "list",
                                 "create-patched", "remove-patched"],
                        help="create: @Mods/@Patches/@Path | create-patched: @patched (after Apply) | "
                             "remove: all nav | remove-patched: @patched only (before Clear) | list: show all")
    args = parser.parse_args()

    print("=" * 70)
    print(f"Aavikko Symlinks — {args.action}")
    print("=" * 70)

    if args.action == "create":
        # Only create @patched symlinks (for C# patches) — @Mods/@Patches/@Path removed (too noisy)
        total_links = 0
        total_fallbacks = 0
        for overlay_root, label, build_target in OVERLAY_PAIRS:
            if not overlay_root.exists():
                continue
            print(f"\n--- {label} ({overlay_root.name}) — @patched ---")
            links, fallbacks = create_patched_links(overlay_root, label, build_target)
            total_links += links
            total_fallbacks += fallbacks

        print()
        print("=" * 70)
        print(f"Created: {total_links} symlinks, {total_fallbacks} text fallbacks")
        if total_fallbacks > 0:
            print("  (Text fallbacks created because symlinks failed —")
            print("   on Windows, enable Developer Mode or run as admin for real symlinks)")
        print("=" * 70)

    elif args.action == "remove":
        # Remove @Mods/@Patches + @Path (NOT @patched — those are Apply/Clear managed)
        total_removed = 0
        for overlay_root, label, _ in OVERLAY_PAIRS:
            if not overlay_root.exists():
                continue
            print(f"\n--- {label} ({overlay_root.name}) ---")
            total_removed += remove_nav_links_for_pair(overlay_root, label)
            total_removed += remove_path_links_for_overlay(overlay_root, label)
        print()
        print("=" * 70)
        print(f"Removed: {total_removed} nav links")
        print("=" * 70)

    elif args.action == "create-patched":
        # Create @patched symlinks (called by Apply.py after successful apply)
        total_links = 0
        total_fallbacks = 0
        for overlay_root, label, build_target in OVERLAY_PAIRS:
            if not overlay_root.exists():
                continue
            print(f"\n--- {label} ({overlay_root.name}) — @patched ---")
            links, fallbacks = create_patched_links(overlay_root, label, build_target)
            total_links += links
            total_fallbacks += fallbacks
        print()
        print("=" * 70)
        print(f"Created: {total_links} @patched symlinks, {total_fallbacks} fallbacks")
        print("=" * 70)

    elif args.action == "remove-patched":
        # Remove @patched symlinks (called by Clear.py before reverting)
        total_removed = 0
        for overlay_root, label, _ in OVERLAY_PAIRS:
            if not overlay_root.exists():
                continue
            print(f"\n--- {label} ({overlay_root.name}) ---")
            total_removed += remove_patched_links(overlay_root, label)
        print()
        print("=" * 70)
        print(f"Removed: {total_removed} @patched symlinks")
        print("=" * 70)

    elif args.action == "list":
        total_found = 0
        for overlay_root, label, _ in OVERLAY_PAIRS:
            if not overlay_root.exists():
                continue
            print(f"\n--- {label} ({overlay_root.name}) ---")
            total_found += list_nav_links_for_pair(overlay_root, label)
        print()
        print("=" * 70)
        print(f"Found: {total_found} nav links")
        print("=" * 70)


if __name__ == "__main__":
    main()
