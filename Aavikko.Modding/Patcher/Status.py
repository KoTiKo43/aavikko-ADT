#!/usr/bin/env python3
"""Status.py — machine-readable status bridge for the Aavikko VS Code extension.

Single authoritative entry point for "what is the current state of the overlay?".
Instead of re-implementing overlay/git logic in JavaScript, the extension calls:

    python3 Status.py --json

and gets one JSON document with everything it needs:

    {
      "schema_version": 1,
      "state": "applied" | "pristine",
      "git": { "branch": ..., "head": ..., "ahead": N, "behind": N,
               "robust": { "head": ..., "branch": ... } | null },
      "applied": { "at": ..., "head_commit": ..., counts, patch lists } | null,
      "overlay": { "content_patches": [...], "content_mods": [...],
                   "robust_patches": [...],  "robust_mods": [...],
                   "resource_patches": [...],"resource_mods": [...] },
      "dirty":   [ { "path", "status", "type", "has_overlay" } ],
      "conflicts": { "patches": [...], "mods": [...] },
      "decisions": { "patches": {...}, "mods": {...} },
      "baseline_recorded": true | false
    }

Design:
  - READ-ONLY: never writes state files, never touches git index.
  - NEVER crashes: any internal error is reported as { "error": "..." } with
    exit code 0, so the extension can always parse stdout.
  - Human-readable mode (no --json) prints a pretty summary via ui.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Force UTF-8 for stdout/stderr — Windows default is cp1251 which can't encode
# Unicode characters in paths/messages. Without this, Status.py crashes with
# UnicodeEncodeError when run on Windows PowerShell (and VS Code extension
# receives empty stdout → "Unexpected end of JSON input" parse error).
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_ROOT = SCRIPT_DIR.parent.parent
ROBUST_DIR = BUILD_ROOT / "RobustToolbox"
APPLIED_FILE = SCRIPT_DIR / ".applied"

SCHEMA_VERSION = 1

# Directories scanned for dirty (modified-since-HEAD) files.
# Mirrors Generate.py list_modified_all() semantics.
CONTENT_PREFIXES = (
    "Content.Server/", "Content.Client/", "Content.Shared/",
    "Content.Server.Database/", "Content.Tests/", "Content.IntegrationTests/",
    "Content.Packaging/", "Content.MapRenderer/", "Content.YAMLLinter/",
    "Content.Benchmarks/", "Content.Tools/",
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def run(cmd: list[str], cwd: Path | None = None) -> tuple[str, str, int]:
    """Run a command (argv list — no shell). Returns (stdout, stderr, rc)."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return "", str(e), 127


def detect_path_type(path: str) -> str:
    """Route a relative path to its overlay type (mirrors Generate.py)."""
    if path.startswith("Resources/"):
        return "resources"
    if path.startswith("RobustToolbox/"):
        return "robust"
    return "content"


def is_dirty_candidate(status: str, path: str) -> bool:
    """Filter for git status --porcelain entries (mirrors Generate.py)."""
    if not status or status[0] not in ("M", "A", "?"):
        return False
    if path.startswith("Aavikko."):
        return False
    # RobustToolbox is scanned separately (its own git repo)
    if path == "RobustToolbox" or path.startswith("RobustToolbox/"):
        return False
    if path.endswith(".csproj"):
        return False
    return True


def collect_dirty() -> list[dict]:
    """Modified/added/untracked files in build root + RobustToolbox submodule."""
    dirty: list[dict] = []

    def _parse_porcelain(stdout: str, prefix: str = "") -> None:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            status = parts[0].strip()
            path = parts[1].strip().split(" -> ")[-1].strip('"')
            if path.endswith("/"):  # collapsed untracked dir — not a file
                continue
            if not is_dirty_candidate(status, path):
                continue
            full = f"{prefix}{path}"
            dirty.append({
                "path": full,
                "status": status,
                "type": detect_path_type(full),
                "has_overlay": has_overlay(full),
            })

    out, _, rc = run(["git", "status", "--porcelain", "--untracked-files=all"],
                     cwd=BUILD_ROOT)
    if rc == 0:
        _parse_porcelain(out)

    if ROBUST_DIR.exists() and (ROBUST_DIR / ".git").exists():
        out_rb, _, rc_rb = run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROBUST_DIR)
        if rc_rb == 0:
            _parse_porcelain(out_rb, prefix="RobustToolbox/")

    return dirty


def has_overlay(rel_path: str) -> bool:
    """Check if an overlay file (patch or mod) exists for the given upstream path."""
    if rel_path.startswith("Resources/"):
        mirror = rel_path[len("Resources/"):]
        return (
            (BUILD_ROOT / "Aavikko.Resources" / "Patches" / mirror).exists()
            or (BUILD_ROOT / "Aavikko.Resources" / "Mods" / mirror).exists()
        )
    if rel_path.startswith("RobustToolbox/"):
        inner = rel_path[len("RobustToolbox/"):]
        base = BUILD_ROOT / "Aavikko.RobustToolbox"
        return (
            (base / "Patches" / f"{inner}.patch").exists()
            or (base / "Mods" / inner).exists()
        )
    base = BUILD_ROOT / "Aavikko.Content"
    return (
        (base / "Patches" / f"{rel_path}.patch").exists()
        or (base / "Mods" / rel_path).exists()
    )


def scan_overlay_dir(root: Path, suffixes: tuple[str, ...] | None) -> list[str]:
    """Recursively list overlay files (relative paths), skipping nav artifacts."""
    if not root.exists():
        return []
    result: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.name
        # Skip symlink-nav artifacts and metadata (mirrors extension logic)
        if name.startswith(".") or name.startswith("@") or name.endswith(".broken"):
            continue
        if suffixes and not any(name.endswith(s) for s in suffixes):
            continue
        result.append(p.relative_to(root).as_posix())
    return result


def collect_overlay() -> dict:
    """Inventory of all overlay files, grouped by root."""
    return {
        "content_patches": scan_overlay_dir(
            BUILD_ROOT / "Aavikko.Content" / "Patches", (".patch",)),
        "content_mods": scan_overlay_dir(
            BUILD_ROOT / "Aavikko.Content" / "Mods", (".cs", ".xaml")),
        "robust_patches": scan_overlay_dir(
            BUILD_ROOT / "Aavikko.RobustToolbox" / "Patches", (".patch",)),
        "robust_mods": scan_overlay_dir(
            BUILD_ROOT / "Aavikko.RobustToolbox" / "Mods", (".cs", ".xaml")),
        "resource_patches": scan_overlay_dir(
            BUILD_ROOT / "Aavikko.Resources" / "Patches", None),
        "resource_mods": scan_overlay_dir(
            BUILD_ROOT / "Aavikko.Resources" / "Mods", None),
    }


def collect_git_info() -> dict:
    """Branch/HEAD of the main repo and the RobustToolbox submodule."""
    branch, _, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=BUILD_ROOT)
    head, _, _ = run(["git", "rev-parse", "HEAD"], cwd=BUILD_ROOT)

    info: dict = {"branch": branch, "head": head, "robust": None}

    # Ahead/behind vs upstream tracking branch (if configured)
    ab, _, rc = run(
        ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        cwd=BUILD_ROOT,
    )
    if rc == 0 and ab:
        parts = ab.split()
        if len(parts) == 2:
            try:
                info["behind"], info["ahead"] = int(parts[0]), int(parts[1])
            except ValueError:
                pass

    if ROBUST_DIR.exists() and (ROBUST_DIR / ".git").exists():
        rb_head, _, _ = run(["git", "rev-parse", "HEAD"], cwd=ROBUST_DIR)
        rb_branch, _, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROBUST_DIR)
        info["robust"] = {"head": rb_head, "branch": rb_branch}

    return info


def collect_applied() -> dict | None:
    """Read .applied marker (written by Apply.py)."""
    if not APPLIED_FILE.exists():
        return None
    try:
        data = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"corrupted": True}
    return {
        "at": data.get("applied_at"),
        "head_commit": data.get("head_commit"),
        "cs_patches_applied": data.get("cs_patches_applied", []),
        "robust_patches_applied": data.get("robust_patches_applied", []),
        "cs_patches_failed": data.get("cs_patches_failed", []),
        "cs_patches_skipped": data.get("cs_patches_skipped", []),
        "counts": {
            "patches_copied": data.get("patches_copied", 0),
            "mods_copied": data.get("mods_copied", 0),
            "content_mods_copied": data.get("content_mods_copied", 0),
            "robust_mods_copied": data.get("robust_mods_copied", 0),
        },
    }


def collect_conflicts() -> dict:
    """Reuse Check.py logic (import — no subprocess, no state writes)."""
    empty = {
        "baseline_recorded": False,
        "upstream_commit": "",
        "old_commit": "",
        "conflicts": {"patches": [], "mods": []},
        "decisions": {"patches": {}, "mods": {}},
    }
    try:
        import Check  # noqa: local import — sibling module
        old_state = Check.load_state()
        if not old_state:
            return empty
        decisions = Check.load_decisions()
        new_state = Check.collect_current_state()
        patches_conflicts, mods_conflicts = Check.detect_conflicts(
            old_state, new_state, decisions)
        return {
            "baseline_recorded": True,
            "upstream_commit": new_state.get("upstream_commit", ""),
            "old_commit": old_state.get("upstream_commit", ""),
            "conflicts": {"patches": patches_conflicts, "mods": mods_conflicts},
            "decisions": {
                "patches": decisions.get("patches", {}),
                "mods": decisions.get("mods", {}),
            },
        }
    except Exception:
        return empty


def collect_status() -> dict:
    """Full status document."""
    applied = collect_applied()
    overlay = collect_overlay()
    conflicts = collect_conflicts()
    return {
        "schema_version": SCHEMA_VERSION,
        "build_root": str(BUILD_ROOT),
        "state": "applied" if applied else "pristine",
        "git": collect_git_info(),
        "applied": applied,
        "overlay": overlay,
        "overlay_counts": {k: len(v) for k, v in overlay.items()},
        "dirty": collect_dirty(),
        **conflicts,
    }


# ── Human-readable output ───────────────────────────────────────────────────


def print_human(status: dict) -> None:
    try:
        from ui import header, kv, ok, warn, info, bold, cyan
    except ImportError:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return

    header("Aavikko Status", "overlay state summary")
    kv("State", bold(status["state"].upper()))
    git = status["git"]
    kv("Branch", git.get("branch", "?"))
    kv("HEAD", (git.get("head") or "?")[:12])
    if git.get("robust"):
        kv("RobustToolbox", git["robust"]["head"][:12])

    oc = status["overlay_counts"]
    print()
    kv("Content patches", oc["content_patches"])
    kv("Content mods", oc["content_mods"])
    kv("Robust patches", oc["robust_patches"])
    kv("Robust mods", oc["robust_mods"])
    kv("Resource patches", oc["resource_patches"])
    kv("Resource mods", oc["resource_mods"])

    dirty = status["dirty"]
    print()
    if dirty:
        warn(f"{len(dirty)} modified file(s) not yet captured:")
        for d in dirty[:15]:
            tag_mark = cyan("[overlay]") if d["has_overlay"] else "[new]"
            print(f"    {tag_mark} {d['path']}")
        if len(dirty) > 15:
            info(f"... and {len(dirty) - 15} more")
        info("Run: python3 Generate.py --all")
    else:
        ok("No uncaptured changes.")

    n_conf = len(status["conflicts"]["patches"]) + len(status["conflicts"]["mods"])
    print()
    if n_conf:
        warn(f"{n_conf} unresolved conflict(s) — run: python3 Check.py")
    else:
        ok("No conflicts.")


# ── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aavikko overlay status (JSON bridge for the VS Code extension)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output")
    args = parser.parse_args()

    try:
        status = collect_status()
    except Exception as e:  # never crash — extension always parses stdout
        status = {"schema_version": SCHEMA_VERSION, "error": f"{type(e).__name__}: {e}"}

    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        print_human(status)


if __name__ == "__main__":
    main()
