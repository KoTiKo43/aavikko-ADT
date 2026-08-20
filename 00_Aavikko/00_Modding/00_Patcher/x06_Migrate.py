#!/usr/bin/env python3
"""
Migrate.py — generate 00_Aavikko/01_Resources/Mods + Patches + manifest.yml
from diff(Aavikko-2.0/Resources, Corvax_Clean/Resources).

Output structure (in CORVAX_DST/00_Aavikko/01_Resources/):
  Mods/         — new Aavikko-only files (original paths preserved verbatim)
  Patches/      — modified upstream files (mirror path)
  manifest.yml  — delete + stale lists
  Deletes/      — (developer-maintained, not touched by Migrate)

Why "preserve paths verbatim": the SS14 engine resolves sprite paths relative
to Resources/. For example `sprite: Aavikko/Clothing/Foo.rsi` → engine looks at
`Resources/Textures/Aavikko/Clothing/Foo.rsi`. So Aavikko source files must be
placed at `Resources/<original_path>/` verbatim — no Aavikko/ prepending,
no path rewriting.

Steps:
  1. diff -rq Aavikko-2.0/Resources vs Corvax/Resources
  2. Copy new files → Mods/
  3. Copy modified files → Patches/
  4. Deduplicate FTL keys in Mods/Locale/ AND Patches/Locale/
  5. Find prototype ID conflicts (for manifest.yml delete list)
  6. Write manifest.yml + copy DB migrations
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from l01_ui import (
    header, section, divider, kv, ok, info, warn, error, fatal,
    skip, hint, tag, bullet, progress_iter, summary_table,
    success_banner, fail_banner, dim, bold,
    green, yellow, red, cyan, magenta,
)

# Compute default paths relative to THIS script file.
# Script lives at: <build_root>/00_Aavikko/00_Modding/Patcher/Migrate.py
# That's 3 levels deep inside build_root, so:
#   parent       = .../Patcher/
#   parent²      = .../00_Aavikko/00_Modding/
#   parent³      = .../Corvax_Clean/   ← build_root
#   parent⁴      = .../ss14_builds/    ← builds_root (contains Corvax_Clean/ + Aavikko-2.0/)
_SCRIPT_PATH = Path(__file__).resolve().parent  # directory of this script
_BUILD_ROOT = _SCRIPT_PATH.parent.parent.parent  # 00_Patcher → 00_Modding → 00_Aavikko → build_root
_BUILDS_ROOT = _BUILD_ROOT.parent                 # contains Corvax_Clean/, Aavikko-2.0/, etc.

DEFAULT_AAVIKKO_SRC = str(_BUILDS_ROOT / "Aavikko-Avaruus-ADT" / "Resources")
DEFAULT_CORVAX_DST = str(_BUILD_ROOT)  # Aavikko-Avaruus-ADT-overlay (has upstream ADT Resources/)

TEXTUAL_EXT = {".yml", ".yaml", ".ftl", ".json", ".xml", ".txt", ".csv", ".toml", ".ini", ".lua", ".js", ".ts", ".cs"}
ID_RE = re.compile(r"""^\s*-?\s*id:\s*['"]?([^'"\s#]+)""", re.MULTILINE)
FTL_KEY_RE = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_-]*)\s*=", re.MULTILINE)
FTL_TOP_KEY_RE = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_-]*)\s*=")


def run(cmd: str, cwd: Path | None = None) -> tuple[str, str, int]:
    result = subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXTUAL_EXT


def collect_prototype_ids(directory: Path) -> set[str]:
    """Collect all 'id:' values from .yml/.yaml files in directory.
    Only scans prototype files (.yml with 'type:' in content)."""
    ids: set[str] = set()
    if not directory.exists():
        return ids
    for f in directory.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".yml", ".yaml"):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Only scan files that look like prototypes (contain 'type:')
        if "type:" not in content:
            continue
        for m in ID_RE.finditer(content):
            ids.add(m.group(1))
    return ids


def get_diff(aavikko_src: Path, corvax_res: Path) -> list[tuple[str, str, str]]:
    stdout, stderr, rc = run(f"diff -rq --no-dereference {aavikko_src} {corvax_res}")
    if rc == 2:
        error(f"diff failed: {stderr}")
        sys.exit(1)
    results: list[tuple[str, str, str]] = []
    aavikko_src_str = str(aavikko_src)
    corvax_res_str = str(corvax_res)
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("Only in"):
            try:
                left, filename = line.rsplit(": ", 1)
            except ValueError:
                continue
            dir_path_str = left.replace("Only in ", "")
            full = Path(dir_path_str) / filename
            is_aavikko = aavikko_src_str in dir_path_str
            if full.is_dir():
                base = aavikko_src if is_aavikko else corvax_res
                for f in full.rglob("*"):
                    if f.is_file():
                        rel = str(f.relative_to(base))
                        if is_aavikko:
                            results.append(("new", rel, ""))
                        else:
                            results.append(("corvax_only", "", rel))
            elif full.is_file():
                if is_aavikko:
                    rel = str(full.relative_to(aavikko_src))
                    results.append(("new", rel, ""))
                else:
                    rel = str(full.relative_to(corvax_res))
                    results.append(("corvax_only", "", rel))
        elif line.startswith("Files ") and "differ" in line:
            try:
                left, right = line.split(" and ", 1)
                aavikko_path = left.replace("Files ", "")
                corvax_path = right.replace(" differ", "")
            except ValueError:
                continue
            aavikko_rel = str(Path(aavikko_path).relative_to(aavikko_src))
            corvax_rel = str(Path(corvax_path).relative_to(corvax_res))
            results.append(("modified", aavikko_rel, corvax_rel))
    return results


def safe_copy(src: Path, dst: Path, src_root: Path | None = None) -> bool:
    """Copy file safely. If src is a symlink, check it doesn't escape src_root.

    - Regular files: shutil.copy (fresh mtime)
    - Symlinks: warn and skip (symlinks in SS14 resources are unexpected)
      If src_root is provided, also check symlink target is inside src_root.
    """
    if src.is_symlink():
        link_target = os.readlink(str(src))
        target_resolved = (src.parent / link_target).resolve()
        if src_root:
            try:
                target_resolved.relative_to(src_root.resolve())
            except ValueError:
                warn(f"Symlink escapes source tree: {src} → {link_target}")
                return False
        warn(f"Symlink found, skipping (not copying): {src} → {link_target}")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    return True


def migrate_new(rel_path: str, aavikko_src: Path, mods_dir: Path) -> bool:
    src = aavikko_src / rel_path
    if not src.exists() or src.is_dir():
        return False
    dst = mods_dir / rel_path
    try:
        return safe_copy(src, dst, aavikko_src)
    except OSError as e:
        warn(f"copy failed: {rel_path} → {e}")
        return False


def migrate_modified(rel_path: str, aavikko_src: Path, patches_dir: Path) -> bool:
    src = aavikko_src / rel_path
    if not src.exists() or src.is_dir():
        return False
    dst = patches_dir / rel_path
    try:
        return safe_copy(src, dst, aavikko_src)
    except OSError as e:
        warn(f"copy failed: {rel_path} → {e}")
        return False


def collect_ftl_keys_by_file(directory: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    if not directory.exists():
        return result
    for f in directory.rglob("*.ftl"):
        if not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(f.relative_to(directory))
        result[rel] = set(m.group(1) for m in FTL_KEY_RE.finditer(content))
    return result


def _remove_keys_from_ftl(file_path: Path, keys_to_remove: set[str]) -> tuple[int, bool]:
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        return (0, False)
    lines = content.splitlines(keepends=True)
    kept_lines = []
    removed = 0
    skip_indented = False
    for line in lines:
        m = FTL_TOP_KEY_RE.match(line)
        if m:
            key = m.group(1)
            if key in keys_to_remove:
                removed += 1
                skip_indented = True
                continue
            skip_indented = False
            kept_lines.append(line)
        else:
            if skip_indented:
                continue
            kept_lines.append(line)
    if removed == 0:
        return (0, False)
    new_content = "".join(kept_lines).rstrip() + "\n"
    if not new_content.strip():
        file_path.unlink()
        return (removed, True)
    file_path.write_text(new_content, encoding="utf-8")
    return (removed, False)


def deduplicate_locale_keys(mods_dir: Path, patches_dir: Path, corvax_res: Path,
                            deleted_files: list[str] | None = None) -> int:
    mods_locale = mods_dir / "Locale"
    patches_locale = patches_dir / "Locale"
    upstream_locale = corvax_res / "Locale"
    if deleted_files is None:
        deleted_files = []
    if not mods_locale.exists() and not patches_locale.exists():
        return 0
    upstream_keys_all: set[str] = set()
    upstream_keys_by_file: dict[str, set[str]] = {}
    if upstream_locale.exists():
        upstream_keys_by_file = collect_ftl_keys_by_file(upstream_locale)
        for keys in upstream_keys_by_file.values():
            upstream_keys_all |= keys
    total_removed = 0
    files_modified = 0
    files_deleted = 0

    if mods_locale.exists():
        patches_keys_all: set[str] = set()
        if patches_locale.exists():
            for keys in collect_ftl_keys_by_file(patches_locale).values():
                patches_keys_all |= keys
        for f in sorted(mods_locale.rglob("*.ftl")):
            if not f.is_file():
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except OSError:
                continue
            keys_in_file = set(m.group(1) for m in FTL_KEY_RE.finditer(content))
            keys_to_remove = {k for k in keys_in_file if k in upstream_keys_all or k in patches_keys_all}
            if not keys_to_remove:
                continue
            removed, deleted = _remove_keys_from_ftl(f, keys_to_remove)
            if removed > 0:
                total_removed += removed
                files_modified += 1
                rel = f.relative_to(mods_dir).as_posix()
                if deleted:
                    files_deleted += 1
                    deleted_files.append(rel)
                    tag("DEDUP-MODS", f"{rel}: removed {removed} → file deleted (empty)", indent=4)
                else:
                    tag("DEDUP-MODS", f"{rel}: removed {removed}", indent=4)

    if patches_locale.exists() and upstream_keys_by_file:
        patches_keys_by_file = collect_ftl_keys_by_file(patches_locale)
        for rel_path, keys_in_patches in patches_keys_by_file.items():
            if not keys_in_patches:
                continue
            same_file_upstream = upstream_keys_by_file.get(rel_path, set())
            keys_to_remove: set[str] = set()
            for key in keys_in_patches:
                if key in same_file_upstream:
                    continue
                if key in upstream_keys_all:
                    keys_to_remove.add(key)
            if not keys_to_remove:
                continue
            f = patches_locale / rel_path
            if not f.is_file():
                continue
            removed, deleted = _remove_keys_from_ftl(f, keys_to_remove)
            if removed > 0:
                total_removed += removed
                files_modified += 1
                if deleted:
                    files_deleted += 1
                    deleted_files.append(rel_path)
                    tag("DEDUP-PATCH", f"{rel_path}: removed {removed} → file deleted (empty)", indent=4)
                else:
                    tag("DEDUP-PATCH", f"{rel_path}: removed {removed}", indent=4)

    if total_removed > 0:
        info(f"Total: removed {total_removed} keys from {files_modified} files ({files_deleted} deleted)")
    return total_removed


def find_conflicts(aavikko_src: Path, corvax_res: Path, patches_dir: Path,
                   mods_dir: Path, aavikko_ids: set[str]) -> list[str]:
    import filecmp
    modified_paths: set[str] = set()
    if patches_dir.exists():
        for f in patches_dir.rglob("*"):
            if f.is_file():
                modified_paths.add(str(f.relative_to(patches_dir)))
    conflicts: set[str] = set()
    scanned = 0
    for yml in corvax_res.rglob("*"):
        if not yml.is_file() or not is_text_file(yml):
            continue
        rel = str(yml.relative_to(corvax_res))
        if rel in modified_paths:
            continue
        scanned += 1
        # Progress is reported by the caller via progress_iter — no per-file print here
        try:
            content = yml.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        file_ids = set(m.group(1) for m in ID_RE.finditer(content))
        if not file_ids or not (file_ids & aavikko_ids):
            continue
        aavikko_version = aavikko_src / rel
        if aavikko_version.exists():
            try:
                if filecmp.cmp(str(yml), str(aavikko_version), shallow=False):
                    continue
            except OSError:
                pass
        conflicts.add(rel)
    return sorted(conflicts)


def copy_db_migrations(aavikko_src: Path, corvax_dst: Path) -> int:
    """Copy Aavikko-specific DB migration FILES (new files, not modifications).

    Copies all migration .cs files from Aavikko source that DON'T exist in
    upstream Corvax. ModelSnapshot.cs is NOT copied (handled by .cs.patch).
    """
    aavikko_migrations = aavikko_src.parent / "Content.Server.Database" / "Migrations"
    corvax_migrations = corvax_dst / "Content.Server.Database" / "Migrations"
    if not aavikko_migrations.exists() or not corvax_migrations.exists():
        return 0
    copied = 0
    for ctx in ("Sqlite", "Postgres"):
        src_dir = aavikko_migrations / ctx
        dst_dir = corvax_migrations / ctx
        if not src_dir.exists() or not dst_dir.exists():
            continue
        # Get upstream file names (for comparison)
        upstream_names = {f.name for f in dst_dir.glob("*.cs")}
        for src in src_dir.glob("*.cs"):
            if not src.is_file():
                continue
            # Skip ModelSnapshot — handled by .cs.patch
            if "ModelSnapshot" in src.name:
                continue
            # Skip if file already exists in upstream (it's a modification, not new)
            if src.name in upstream_names:
                continue
            dst = dst_dir / src.name
            need = True
            if dst.exists():
                try:
                    if src.read_bytes() == dst.read_bytes():
                        need = False
                except OSError:
                    pass
            if need:
                shutil.copy(src, dst)
                copied += 1
                print(f"    [MIGRATION] {ctx}/{src.name}")
    return copied


# ── Content.* migration (C# code) ──────────────────────────────────────────

# Directories to scan for C# modifications (relative to build root)
CONTENT_DIRS = ["Content.Server", "Content.Client", "Content.Shared",
                "Content.Server.Database", "Content.Tests", "Content.IntegrationTests"]

# Sandbox-specific files that are managed by deploy_patch, not by 00_Aavikko/02_Content.
# These must NOT be migrated to 00_Aavikko/02_Content/Mods/ — they'd cause duplicate definitions.
SANDBOX_EXCLUDED_PATHS = {
    "AiApi",      # Content.Server/AiApi/ — deployed via sandbox deploy_patch
    "AiVision",   # Content.Client/AiVision/ — deployed via sandbox deploy_patch
}


def generate_csproj_patches(corvax_dst: Path) -> int:
    """Generate 999-csproj-include-aavikko.cs.patch for each Content.* project
    that has 00_Aavikko/02_Content/Mods/<project>/Aavikko/ files.

    These patches add <Compile Include="..\\00_Aavikko/02_Content\\Mods\\<project>\\Aavikko\\**\\*.cs" />
    to each csproj so the SDK-style project picks up Aavikko C# files from the overlay.

    Without these patches, the compiler won't see 00_Aavikko/02_Content/Mods/ files and
    will fail with CS0234 "type or namespace 'Aavikko' does not exist".

    Returns: number of csproj patches generated.
    """
    content_patches_dir = corvax_dst / "00_Aavikko/02_Content" / "Patches"
    content_mods_dir = corvax_dst / "00_Aavikko/02_Content" / "Mods"
    generated = 0

    # Standard SS14 Content.* projects that may have Aavikko code
    projects = ["Content.Shared", "Content.Server", "Content.Client",
                "Content.Server.Database", "Content.Tests", "Content.IntegrationTests"]

    for proj in projects:
        aavikko_dir = content_mods_dir / proj / "Aavikko"
        if not aavikko_dir.exists():
            continue
        cs_count = sum(1 for _ in aavikko_dir.rglob("*.cs"))
        if cs_count == 0:
            continue

        csproj_path = corvax_dst / proj / f"{proj}.csproj"
        if not csproj_path.exists():
            continue

        # Read csproj, check if already has Aavikko reference
        content = csproj_path.read_text(encoding="utf-8")
        if "00_Aavikko/02_Content" in content:
            continue  # Already has reference, skip

        # Insert ItemGroup before </Project>
        include_path = f"..\\\\00_Aavikko/02_Content\\\\Mods\\\\{proj}\\\\Aavikko\\\\**\\\\*.cs"
        new_content = content.replace(
            "</Project>",
            f"\n  <ItemGroup>\n    <Compile Include=\"{include_path}\" />\n  </ItemGroup>\n</Project>"
        )
        csproj_path.write_text(new_content, encoding="utf-8")

        # Capture git diff
        diff_result = subprocess.run(
            f"git diff -- {proj}/{proj}.csproj",
            shell=True, cwd=corvax_dst, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )

        # Restore csproj
        run(f"git checkout -- {proj}/{proj}.csproj", cwd=corvax_dst)

        if not diff_result.stdout.strip():
            continue

        # Save patch
        patch_dest = content_patches_dir / proj / "999-csproj-include-aavikko.cs.patch"
        patch_dest.parent.mkdir(parents=True, exist_ok=True)
        patch_dest.write_text(diff_result.stdout + "\n", encoding="utf-8")

        # Verify patch applies cleanly
        patch_abs = patch_dest.resolve()
        _, _, verify_rc = run(
            f"git apply --check {shlex.quote(str(patch_abs))}",
            cwd=corvax_dst
        )
        if verify_rc == 0:
            tag("CSPROJ-PATCH", f"{proj}/999-csproj-include-aavikko.cs.patch ({cs_count} .cs files)",
                indent=4, color=green)
            generated += 1
        else:
            patch_dest.unlink()
            tag("CSPROJ-FAIL", f"{proj}/999-csproj-include-aavikko.cs.patch — verification failed, deleted",
                indent=4, color=red)

    return generated


def migrate_content(aavikko_build: Path, corvax_dst: Path) -> tuple[int, int]:
    """Migrate C# modifications from Aavikko-2.0 to 00_Aavikko/02_Content/Patches/ + Mods/.

    For each Content.* directory:
    - Modified files → generate .cs.patch via git diff (copy Aavikko version,
      capture diff, restore upstream)
    - New Aavikko-only files → copy to 00_Aavikko/02_Content/Mods/<mirror_path>

    Returns: (patches_generated, mods_copied)
    """
    content_patches_dir = corvax_dst / "00_Aavikko/02_Content" / "Patches"
    content_mods_dir = corvax_dst / "00_Aavikko/02_Content" / "Mods"

    patches_generated = 0
    mods_copied = 0

    for content_dir in CONTENT_DIRS:
        aavikko_content = aavikko_build / content_dir
        corvax_content = corvax_dst / content_dir

        if not aavikko_content.exists() or not corvax_content.exists():
            continue

        # Get diff
        stdout, _, _ = run(f"diff -rq --no-dereference {aavikko_content} {corvax_content}")
        aavikko_str = str(aavikko_content)
        corvax_str = str(corvax_content)

        for line in stdout.splitlines():
            if not line.strip():
                continue

            if line.startswith("Only in") and aavikko_str in line:
                # New Aavikko-only file → 00_Aavikko/02_Content/Mods/
                try:
                    left, filename = line.rsplit(": ", 1)
                except ValueError:
                    continue
                dir_path = Path(left.replace("Only in ", ""))
                full = dir_path / filename

                if full.is_dir():
                    for f in full.rglob("*"):
                        if f.is_file():
                            rel = str(f.relative_to(aavikko_content))
                            # Skip sandbox-specific files (managed by deploy_patch)
                            if any(rel.startswith(ex) or f"/{ex}/" in rel for ex in SANDBOX_EXCLUDED_PATHS):
                                continue
                            dst = content_mods_dir / content_dir / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy(f, dst)
                            mods_copied += 1
                elif full.is_file():
                    rel = str(full.relative_to(aavikko_content))
                    # Skip sandbox-specific files (managed by deploy_patch)
                    if any(rel.startswith(ex) or f"/{ex}/" in rel for ex in SANDBOX_EXCLUDED_PATHS):
                        continue
                    dst = content_mods_dir / content_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(full, dst)
                    mods_copied += 1
                    tag("CONTENT-MOD", f"{content_dir}/{rel}", indent=4, color=cyan)

            elif line.startswith("Files ") and "differ" in line:
                # Modified upstream file → generate .cs.patch
                try:
                    left, right = line.split(" and ", 1)
                    aavikko_path = left.replace("Files ", "")
                    corvax_path = right.replace(" differ", "")
                except ValueError:
                    continue

                rel = str(Path(aavikko_path).relative_to(aavikko_content))

                # Skip obj/bin directories
                if "/obj/" in aavikko_path or "/bin/" in aavikko_path:
                    continue

                # Copy Aavikko version to upstream (temporary)
                target = corvax_content / rel
                shutil.copy(aavikko_path, target)

                try:
                    # Capture git diff — CRITICAL: do NOT use run() which strips
                    # trailing whitespace. git diff output includes trailing empty
                    # context lines that are part of the patch format. Stripping
                    # them produces "corrupt patch at line N" errors in git apply.
                    diff_result = subprocess.run(
                        f"git diff -- {content_dir}/{rel}",
                        shell=True, cwd=corvax_dst, capture_output=True,
                        text=True, encoding="utf-8", errors="replace"
                    )
                    diff_stdout = diff_result.stdout

                    # Restore upstream FIRST (before saving/verifying patch)
                    run(f"git checkout -- {content_dir}/{rel}", cwd=corvax_dst)

                    if diff_stdout.strip():
                        # Save patch — preserve trailing whitespace from git diff
                        patch_dest = content_patches_dir / content_dir / f"{rel}.patch"
                        patch_dest.parent.mkdir(parents=True, exist_ok=True)
                        patch_dest.write_text(diff_stdout + "\n", encoding="utf-8")

                        # Verify patch (now against clean upstream — should apply cleanly)
                        # STRICT: delete broken patches instead of saving them.
                        # This prevents the "corrupt patch at line N" failures that
                        # silently accumulated in the tree before Validate.py existed.
                        # CRITICAL: use absolute path for patch file — git apply --check
                        # resolves patch path relative to cwd, but we need the patch file
                        # to be found regardless of cwd.
                        patch_abs = patch_dest.resolve()
                        _, verify_err, verify_rc = run(
                            f"git apply --check {shlex.quote(str(patch_abs))}",
                            cwd=corvax_dst
                        )
                        if verify_rc == 0:
                            tag("CONTENT-PATCH", f"{content_dir}/{rel}.patch", indent=4, color=green)
                            patches_generated += 1
                        else:
                            # Delete the broken patch — better no patch than a broken one
                            patch_dest.unlink()
                            tag("CONTENT-FAIL", f"{content_dir}/{rel}.patch — verification FAILED, deleted", indent=4, color=red)
                            hint(f"git apply --check error: {verify_err[:200]}", )
                            hint(f"To save anyway: manually run Generate.py {content_dir}/{rel} --keep-broken")
                except (KeyboardInterrupt, Exception) as e:
                    # Emergency restore — ensure upstream is clean even on crash
                    run(f"git checkout -- {content_dir}/{rel}", cwd=corvax_dst)
                    warn(f"Interrupted while processing {content_dir}/{rel}: {e}")
                    raise

    return (patches_generated, mods_copied)


def count_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*") if _.is_file())


def main():
    parser = argparse.ArgumentParser(description="Aavikko Resources migration")
    parser.add_argument("--clean", action="store_true", help="Wipe Mods/Patches/manifest first")
    parser.add_argument("--aavikko-src", default=os.environ.get("AAVIKKO_SRC", DEFAULT_AAVIKKO_SRC))
    parser.add_argument("--corvax-dst", default=os.environ.get("CORVAX_DST", DEFAULT_CORVAX_DST))
    parser.add_argument("--skip-conflicts", action="store_true")
    parser.add_argument("--skip-migrations", action="store_true", help="Skip DB migration copy")
    parser.add_argument("--skip-content", action="store_true", help="Skip Content.* C# migration")
    parser.add_argument("--only-migrations", action="store_true",
                        help="Only run copy_db_migrations (skip everything else)")
    args = parser.parse_args()

    aavikko_src = Path(args.aavikko_src)
    corvax_dst = Path(args.corvax_dst)
    corvax_res = corvax_dst / "Resources"
    mods_dir = corvax_dst / "00_Aavikko/01_Resources" / "Mods"
    patches_dir = corvax_dst / "00_Aavikko/01_Resources" / "Patches"
    manifest = corvax_dst / "00_Aavikko/01_Resources" / "manifest.yml"

    header("Aavikko Resources Migration")
    kv("Source", aavikko_src)
    kv("Dest", corvax_dst)

    if not aavikko_src.exists():
        print()
        fatal(f"Source not found: {aavikko_src}")
        hint("Check AAVIKKO_SRC env var or --aavikko-src flag.")
        sys.exit(2)
    if not corvax_res.exists():
        print()
        fatal(f"Corvax Resources/ not found: {corvax_res}")
        sys.exit(2)

    # ── --only-migrations: skip everything except DB migration copy ──
    if args.only_migrations:
        print("\n[+] Copying DB migrations (only)...")
        t = time.time()
        copied = copy_db_migrations(aavikko_src, corvax_dst)
        print(f"  Copied {copied} migration files  ({time.time() - t:.1f}s)")
        return

    # ── Check Resources/ is clean ──
    dirty_stdout, _, _ = run("git status --porcelain Resources/", cwd=corvax_dst)
    dirty_files = [l for l in dirty_stdout.splitlines() if l.strip()]
    if dirty_files:
        if args.clean:
            print(f"\n[--clean] Resources/ has {len(dirty_files)} dirty file(s) — auto-cleaning...")
            run("git checkout HEAD -- Resources/", cwd=corvax_dst)
            run("git clean -fd Resources/", cwd=corvax_dst)
            print("  [OK] Resources/ reverted to clean upstream state")
        else:
            print(f"\n[WARNING] Resources/ has {len(dirty_files)} uncommitted change(s)!", file=sys.stderr)
            print("  This will cause Migrate.py to misclassify files (Mods/ → Patches/).", file=sys.stderr)
            print("  Fix: run Clear.py first, OR use --clean flag.", file=sys.stderr)
            print(f"\n  Dirty files (first 5):", file=sys.stderr)
            for f in dirty_files[:5]:
                print(f"    {f}", file=sys.stderr)
            print(f"\n  Continuing anyway in 3s... (Ctrl+C to abort)", file=sys.stderr)
            try:
                time.sleep(3)
            except KeyboardInterrupt:
                sys.exit(1)

    # ── Check Content.* is clean ──
    content_dirty_stdout, _, _ = run(
        "git status --porcelain Content.Server Content.Client Content.Shared "
        "Content.Server.Database Content.Tests Content.IntegrationTests",
        cwd=corvax_dst
    )
    content_dirty = [l for l in content_dirty_stdout.splitlines() if l.strip()]
    if content_dirty:
        if args.clean:
            print(f"\n[--clean] Content.* has {len(content_dirty)} dirty file(s) — auto-cleaning...")
            run("git checkout HEAD -- Content.Server Content.Client Content.Shared "
                "Content.Server.Database Content.Tests Content.IntegrationTests", cwd=corvax_dst)
            run("git clean -fd Content.Server Content.Client Content.Shared "
                "Content.Server.Database Content.Tests Content.IntegrationTests", cwd=corvax_dst)
            print("  [OK] Content.* reverted to clean upstream state")
        else:
            print(f"\n[WARNING] Content.* has {len(content_dirty)} uncommitted change(s)!", file=sys.stderr)
            print("  migrate_content will produce bogus patches.", file=sys.stderr)
            print("  Fix: run Clear.py first, OR use --clean flag.", file=sys.stderr)
            try:
                time.sleep(3)
            except KeyboardInterrupt:
                sys.exit(1)

    if args.clean:
        print("\n[--clean] Wiping previous output...")
        if mods_dir.exists():
            shutil.rmtree(mods_dir)
        if patches_dir.exists():
            shutil.rmtree(patches_dir)
        if manifest.exists():
            manifest.unlink()
        # Also wipe Content/Mods and Content/Patches
        content_mods = corvax_dst / "00_Aavikko/02_Content" / "Mods"
        content_patches = corvax_dst / "00_Aavikko/02_Content" / "Patches"
        if content_mods.exists():
            shutil.rmtree(content_mods)
        if content_patches.exists():
            shutil.rmtree(content_patches)
        # Wipe Check.py state files — they reference sha256 of files that
        # may have just been deleted, so they would produce false "upstream
        # file deleted" conflicts on the next Check.py run.
        patcher_dir = Path(__file__).resolve().parent
        state_file = patcher_dir / ".upstream_state.json"
        decisions_file = patcher_dir / ".conflict_decisions.yml"
        applied_file = patcher_dir / ".applied"
        for sf in (state_file, decisions_file, applied_file):
            if sf.exists():
                sf.unlink()
                print(f"  [DEL] {sf.name}")
        print("  [OK] Previous output wiped (Resources + Content + state files)")

    mods_dir.mkdir(parents=True, exist_ok=True)
    patches_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    # 1. Diff
    section(1, 6, "Analyzing diff")
    t = time.time()
    diff = get_diff(aavikko_src, corvax_res)
    new_files = [d for d in diff if d[0] == "new"]
    modified_files = [d for d in diff if d[0] == "modified"]
    corvax_only = [d for d in diff if d[0] == "corvax_only"]
    kv("New",         f"{len(new_files):>6}")
    kv("Modified",    f"{len(modified_files):>6}")
    kv("Corvax-only", f"{len(corvax_only):>4} (not migrated)")
    print(f"  {dim(f'({time.time() - t:.1f}s)')}")

    # 2. Copy new → Mods/
    section(2, 6, f"Copying {len(new_files)} new files → Mods/")
    t = time.time()
    new_ok = 0
    for _, rel, _ in progress_iter(new_files, label="  Copying", unit="files", update_every=100):
        if migrate_new(rel, aavikko_src, mods_dir):
            new_ok += 1
    ok(f"Copied: {new_ok}/{len(new_files)}  ({time.time() - t:.1f}s)")

    # 3. Copy modified → Patches/
    section(3, 6, f"Copying {len(modified_files)} modified files → Patches/")
    t = time.time()
    mod_ok = 0
    for _, rel, _ in progress_iter(modified_files, label="  Copying", unit="files", update_every=100):
        if migrate_modified(rel, aavikko_src, patches_dir):
            mod_ok += 1
    ok(f"Copied: {mod_ok}/{len(modified_files)}  ({time.time() - t:.1f}s)")

    # 4. Deduplicate FTL keys
    section(4, 6, "Deduplicating FTL keys")
    t = time.time()
    dedup_deleted: list[str] = []
    removed = deduplicate_locale_keys(mods_dir, patches_dir, corvax_res, dedup_deleted)
    ok(f"Removed {removed} duplicate keys  ({time.time() - t:.1f}s)")

    # 5. Find prototype ID conflicts
    conflicts: list[str] = []
    if args.skip_conflicts:
        section(5, 6, "Skipping conflict scan (--skip-conflicts)")
    else:
        section(5, 6, "Finding prototype ID conflicts")
        t = time.time()
        aavikko_ids = collect_prototype_ids(mods_dir / "Prototypes")
        aavikko_ids |= collect_prototype_ids(mods_dir)
        kv("Aavikko IDs", f"{len(aavikko_ids)}  ({time.time() - t:.1f}s)")
        t = time.time()
        # Wrap the conflict scan with a progress-aware iterator
        # find_conflicts internally scans corvax_res.rglob("*"); we surface
        # progress by passing a sentinel and reading from the function's side
        # effect (already removed per-2000 print in find_conflicts).
        conflicts = find_conflicts(aavikko_src, corvax_res, patches_dir, mods_dir, aavikko_ids)
        kv("Conflicts", f"{len(conflicts)}  ({time.time() - t:.1f}s)")
        if conflicts:
            print(f"  {dim('First 20:')}")
            for c in conflicts[:20]:
                bullet(c, indent=4)

    # 6. Write manifest.yml + copy migrations
    section(6, 6, "Writing manifest.yml")
    lines = [
        "# Aavikko manifest",
        f"# Generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        "",
        "# Upstream files to delete (prototype ID conflicts)",
        "delete:",
    ]
    for c in conflicts:
        lines.append(f"  - {c}")
    lines.extend(["", "# Stale files (removed by FTL dedup)", "stale:"])
    for s in dedup_deleted:
        lines.append(f"  - {s}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok(f"Written: {manifest.relative_to(corvax_dst)} ({len(conflicts)} delete, {len(dedup_deleted)} stale)")

    if not args.skip_migrations:
        section("+", None, "Copying DB migrations")
        t = time.time()
        copied = copy_db_migrations(aavikko_src, corvax_dst)
        ok(f"Copied {copied} migration files  ({time.time() - t:.1f}s)")

    if not args.skip_content:
        section("+", None, "Migrating Content.* (C# code)")
        t = time.time()
        aavikko_build = aavikko_src.parent  # Aavikko-2.0/ (parent of Resources/)
        content_patches, content_mods = migrate_content(aavikko_build, corvax_dst)
        # No csproj patches needed — Content Mods are copied to Content.*/Aavikko/
        # by Apply.py, and SDK-style csproj picks them up automatically.
        ok(f"Content: {content_patches} patches, "
           f"{content_mods} mods  ({time.time() - t:.1f}s)")

    mods_count = count_files(mods_dir)
    patches_count = count_files(patches_dir)
    content_patches_count = count_files(corvax_dst / "00_Aavikko/02_Content" / "Patches")
    content_mods_count = count_files(corvax_dst / "00_Aavikko/02_Content" / "Mods")
    total_time = time.time() - t_start
    success_banner(
        f"Migration done in {total_time:.1f}s",
        details=[
            ("Resources/Mods/",     f"{mods_count} files"),
            ("Resources/Patches/",  f"{patches_count} files"),
            ("Content/Mods/",       f"{content_mods_count} files"),
            ("Content/Patches/",    f"{content_patches_count} files"),
            ("manifest.yml",        f"{len(conflicts)} delete, {len(dedup_deleted)} stale"),
        ],
        next_step="python3 00_Aavikko/00_Modding/Patcher/Apply.py",
    )


if __name__ == "__main__":
    main()