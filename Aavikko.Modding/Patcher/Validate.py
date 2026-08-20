#!/usr/bin/env python3
"""
Validate.py — verify ALL .cs.patch and .xaml.patch files apply cleanly.

Scans:
  - Aavikko.Content/Patches/  (auto-generated + manually created .cs/.xaml patches)
  - Aavikko.Modding/Patches/  (dev-maintained patches, including RobustToolbox)

For each .patch file:
  1. Runs `git apply --check` against current upstream (working tree must be clean)
  2. Reports PASS / FAIL with detailed error message
  3. Returns non-zero exit code if ANY patch fails

Use cases:
  - Pre-deploy sanity check (called by deploy_full.py)
  - CI check (run before merge)
  - Manual verification after editing patches
  - Diagnosing "corrupt patch at line N" errors from Apply.py

Usage:
  python3 Validate.py            # Check all patches, exit 0 if all pass
  python3 Validate.py --verbose  # Show every patch (not just failures)
  python3 Validate.py --fix      # Try to auto-regenerate broken patches from
                                   Aavikko-Avaruus-ADT source (if available)
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_ROOT = SCRIPT_DIR.parent.parent
CONTENT_PATCHES_DIR = BUILD_ROOT / "Aavikko.Content" / "Patches"
ROBUST_PATCHES_DIR = BUILD_ROOT / "Aavikko.RobustToolbox" / "Patches"
ROBUST_DIR = BUILD_ROOT / "RobustToolbox"

# Plain print — no ui.py dependency so this can run standalone in CI
def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")

def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", file=sys.stderr)

def warn(msg: str) -> None:
    print(f"  [WARN] {msg}", file=sys.stderr)

def info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def run(cmd: str, cwd: Path | None = None) -> tuple[str, str, int]:
    result = subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def collect_patches() -> list[tuple[Path, Path]]:
    """Find all .cs.patch and .xaml.patch files in Aavikko.Content/Patches/
    and Aavikko.RobustToolbox/Patches/.

    Returns list of (patch_path, cwd) tuples — cwd is BUILD_ROOT for Content.*
    patches, ROBUST_DIR for RobustToolbox patches.
    """
    patches = []
    if CONTENT_PATCHES_DIR.exists():
        for p in sorted(CONTENT_PATCHES_DIR.rglob("*.cs.patch")):
            if not p.name.startswith(".gitkeep"):
                patches.append((p, BUILD_ROOT))
        for p in sorted(CONTENT_PATCHES_DIR.rglob("*.xaml.patch")):
            if not p.name.startswith(".gitkeep"):
                patches.append((p, BUILD_ROOT))
    if ROBUST_PATCHES_DIR.exists() and ROBUST_DIR.exists():
        for p in sorted(ROBUST_PATCHES_DIR.rglob("*.cs.patch")):
            if not p.name.startswith(".gitkeep"):
                patches.append((p, ROBUST_DIR))
        for p in sorted(ROBUST_PATCHES_DIR.rglob("*.xaml.patch")):
            if not p.name.startswith(".gitkeep"):
                patches.append((p, ROBUST_DIR))
    return patches


def check_working_tree_clean() -> tuple[bool, list[str]]:
    """Check if upstream working tree is clean.
    
    Returns (is_clean, dirty_paths).
    Note: Aavikko.* paths are in .gitignore and don't count.
    Migrate.py-copied DB migrations in Content.Server.Database/Migrations/
    are also OK — they don't affect patch validation.
    """
    stdout, _, rc = run("git status --porcelain", cwd=BUILD_ROOT)
    if rc != 0:
        return False, ["<git status failed>"]
    dirty = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        path = line.split(None, 1)[-1].strip().strip('"')
        # Skip Aavikko.* (in .gitignore)
        if path.startswith("Aavikko."):
            continue
        # Skip DB migrations copied by Migrate.py — they're expected after Migrate
        if "Content.Server.Database/Migrations/" in path:
            continue
        if "Content.Server.Database/Model.cs" in path:
            continue
        dirty.append(path)
    return not dirty, dirty


def validate_patch(patch_path: Path, cwd: Path) -> tuple[bool, str]:
    """Run git apply --check on a patch. Returns (success, error_message).

    `cwd` is the working directory for git apply:
      - BUILD_ROOT for Content.* patches (paths start with Content.*/)
      - ROBUST_DIR for RobustToolbox patches (paths start with Robust.*)

    This is the same check Apply.py uses to decide whether to apply a patch.
    If --check fails, Apply.py will also fail. If --check passes, Apply.py
    will apply successfully (in normal circumstances — clean upstream tree).

    IMPORTANT: Validate.py must be run on a CLEAN upstream tree (before Apply).
    Running it after Apply will produce false failures because patches are
    already applied (git apply --check fails on already-applied patches).
    deploy_full.py runs Validate between Migrate and Apply, which is correct.
    """
    patch_str = shlex.quote(str(patch_path))
    _, stderr, rc = run(f"git apply --check {patch_str}", cwd=cwd)
    if rc == 0:
        return True, ""
    return False, stderr[:500]  # truncate long errors


def try_repair_from_aavikko_src(patch_path: Path) -> bool:
    """Attempt to regenerate a broken patch from Aavikko-Avaruus-ADT source.

    Only works if:
      1. Aavikko-Avaruus-ADT source is available (sibling of build root)
      2. The patch corresponds to an upstream .cs/.xaml file
      3. The same file exists in Aavikko-Avaruus-ADT (with Aavikko changes baked in)
    """
    # Determine upstream path from patch filename
    try:
        rel = patch_path.relative_to(CONTENT_PATCHES_DIR)
    except ValueError:
        return False

    upstream_rel = str(rel)
    # Strip .patch suffix to get upstream path
    for suffix in (".cs.patch", ".xaml.cs.patch", ".xaml.patch"):
        if upstream_rel.endswith(suffix):
            upstream_rel = upstream_rel[:-len(suffix)] + suffix[len(".patch"):]
            break
    else:
        return False

    # Try several known Aavikko source locations (relative to BUILD_ROOT)
    # No hard-coded absolute paths — keeps cross-platform compatibility
    aavikko_candidates = [
        BUILD_ROOT.parent / "Aavikko-Avaruus-ADT",
        BUILD_ROOT.parent.parent / "Aavikko-Avaruus-ADT",
    ]
    # Also check AAVIKKO_SRC env var (used by Migrate.py)
    env_src = os.environ.get("AAVIKKO_SRC")
    if env_src:
        # AAVIKKO_SRC points to Resources/, parent is Aavikko-Avaruus-ADT/
        aavikko_candidates.append(Path(env_src).parent)
    aavikko_src = None
    for c in aavikko_candidates:
        if c.exists():
            aavikko_src = c
            break
    if not aavikko_src:
        return False

    aavikko_file = aavikko_src / upstream_rel
    if not aavikko_file.exists():
        return False

    upstream_file = BUILD_ROOT / upstream_rel
    if not upstream_file.exists():
        return False

    # Copy Aavikko version over upstream, capture diff, restore
    import shutil
    shutil.copy2(aavikko_file, upstream_file)
    stdout, _, _ = run(f"git diff -- {shlex.quote(upstream_rel)}", cwd=BUILD_ROOT)
    run(f"git checkout -- {shlex.quote(upstream_rel)}", cwd=BUILD_ROOT)

    if not stdout.strip():
        return False

    # Verify the regenerated patch applies cleanly
    patch_path.write_text(stdout + "\n", encoding="utf-8")
    success, _ = validate_patch(patch_path)
    return success


def main():
    parser = argparse.ArgumentParser(
        description="Validate all .cs.patch and .xaml.patch files"
    )
    parser.add_argument("--verbose", action="store_true",
                        help="Show every patch (not just failures)")
    parser.add_argument("--fix", action="store_true",
                        help="Try to auto-regenerate broken patches from Aavikko-Avaruus-ADT source")
    args = parser.parse_args()

    print("=" * 70)
    print("Aavikko Patch Validator")
    print("=" * 70)

    # Sanity checks
    if not (BUILD_ROOT / ".git").exists():
        print(f"\n[FATAL] BUILD_ROOT is not a git repo: {BUILD_ROOT}", file=sys.stderr)
        sys.exit(2)

    is_clean, dirty_paths = check_working_tree_clean()
    if not is_clean:
        print(f"\n[ERROR] Upstream working tree has {len(dirty_paths)} non-Aavikko change(s).",
              file=sys.stderr)
        for p in dirty_paths[:5]:
            print(f"  - {p}", file=sys.stderr)
        if len(dirty_paths) > 5:
            print(f"  ... and {len(dirty_paths) - 5} more", file=sys.stderr)
        print(f"\nValidate.py must run on CLEAN upstream (before Apply.py).", file=sys.stderr)
        print(f"After Apply.py, patches are already applied → git apply --check fails", file=sys.stderr)
        print(f"on every patch (false positives).", file=sys.stderr)
        print(f"\nFix: run Clear.py first, then Validate.py", file=sys.stderr)
        sys.exit(2)

    patches = collect_patches()
    if not patches:
        print("\n[FATAL] No .cs.patch or .xaml.patch files found.", file=sys.stderr)
        print(f"  Searched: {CONTENT_PATCHES_DIR}", file=sys.stderr)
        print(f"  Searched: {ROBUST_PATCHES_DIR}", file=sys.stderr)
        sys.exit(2)

    print(f"\nFound {len(patches)} patch(es) to validate")
    print()

    passed = 0
    failed = 0
    repaired = 0
    failed_paths = []

    for patch, cwd in patches:
        rel = patch.relative_to(BUILD_ROOT) if str(patch).startswith(str(BUILD_ROOT)) else patch
        success, err = validate_patch(patch, cwd)

        if success:
            passed += 1
            if args.verbose:
                ok(str(rel))
        else:
            failed += 1
            failed_paths.append((patch, err))
            fail(str(rel))
            print(f"         {err.splitlines()[0] if err else '(no error message)'}",
                  file=sys.stderr)

            if args.fix:
                info(f"Attempting auto-repair from Aavikko-Avaruus-ADT source...")
                if try_repair_from_aavikko_src(patch):
                    ok(f"Repaired: {rel}")
                    repaired += 1
                    failed -= 1
                else:
                    warn(f"Could not auto-repair (Aavikko-Avaruus-ADT source not available or file not found)")

    print()
    print("=" * 70)
    print(f"Summary: {passed} passed, {failed} failed" +
          (f", {repaired} repaired" if repaired > 0 else ""))
    print("=" * 70)

    if failed > 0:
        print(f"\nFailed patches:", file=sys.stderr)
        for p, err in failed_paths:
            print(f"  - {p.relative_to(BUILD_ROOT) if str(p).startswith(str(BUILD_ROOT)) else p}",
                  file=sys.stderr)
            print(f"    {err.splitlines()[0] if err else ''}", file=sys.stderr)
        print(f"\nTo fix:", file=sys.stderr)
        print(f"  python3 {SCRIPT_DIR.name}/Validate.py --fix", file=sys.stderr)
        print(f"  (auto-regenerates from Aavikko-Avaruus-ADT source if available)", file=sys.stderr)
        print(f"\nOr manually regenerate:", file=sys.stderr)
        print(f"  python3 {SCRIPT_DIR.name}/Generate.py <upstream_file.cs> --restore",
              file=sys.stderr)
        sys.exit(1)

    print(f"\nAll patches valid. Apply.py can run safely.")


if __name__ == "__main__":
    main()
