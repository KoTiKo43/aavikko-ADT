#!/usr/bin/env python3
"""
publish_server.py — package SS14 server + content bundle into release/server/.

Uses Content.Packaging project (SS14's official packaging tool) to create
a server distribution ZIP in release/.

Output:
  release/
  └── SS14.Server_linux-x64.zip    ← self-contained server package

The ZIP contains:
  - Content.Server.dll + all .NET dependencies
  - Robust.Server.dll + Robust.Shared.dll (compiled from RobustToolbox)
  - Resources/ — including all Aavikko modifications (after Apply)
  - server_config.toml

To run:
  unzip SS14.Server_linux-x64.zip -d server/
  cd server/
  ./Content.Server --config-file server_config.toml

Content bundle (Resources/) is transferred to clients automatically when they
connect via official SS14 launcher. Client receives Aavikko sprites, audio,
prototypes — everything in Resources/.

Requirements:
  - Apply.py must have been run (Resources/ has Aavikko modifications)
  - Build must be up-to-date (Content.Server.dll exists)
  - dotnet restore must have been run for target runtime (linux-x64, win-x64)
    In offline sandbox: this requires runtime packages in NuGet cache

Common issue (offline sandbox):
  "Unable to resolve 'Microsoft.NETCore.App.Runtime.linux-x64'"
  → NuGet cache doesn't have linux-x64 runtime packages.
  → Fix: run on a machine with internet, OR pre-populate NuGet cache:
    dotnet restore --runtime linux-x64
    (needs network access first time)

Usage:
  python3 publish_server.py                           # default: linux-x64, skip-build
  python3 publish_server.py --platform win-x64        # Windows server
  python3 publish_server.py --platform linux-arm64    # ARM64
  python3 publish_server.py --no-skip-build           # rebuild before packaging
  python3 publish_server.py --hybrid-acz              # HybridACZ (smaller client download)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Make sibling ui.py importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from l01_ui import (
    header, section, kv, ok, info, warn, error, hint, tag,
    success_banner, fail_banner, dim, bold,
    green, yellow, red, cyan,
)

SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_ROOT = SCRIPT_DIR.parent.parent.parent
PACKAGING_PROJECT = BUILD_ROOT / "Content.Packaging" / "Content.Packaging.csproj"
RELEASE_DIR = BUILD_ROOT / "release"
APPLIED_FILE = SCRIPT_DIR / ".applied"


def run_cmd(cmd: str, cwd: Path | None = None, timeout: int = 600) -> tuple[str, str, int]:
    """Run a shell command. Returns (stdout, stderr, returncode)."""
    import subprocess
    result = subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def check_prerequisites() -> bool:
    """Check that we can publish: Apply.py ran, build exists, Content.Packaging exists."""
    # 1. Content.Packaging project exists
    if not PACKAGING_PROJECT.exists():
        error(f"Content.Packaging project not found: {PACKAGING_PROJECT}")
        hint("Are you running from the SS14 build root?")
        return False

    # 2. Apply.py must have been run (.applied exists)
    if not APPLIED_FILE.exists():
        error("Apply.py has not been run — Resources/ has no Aavikko modifications")
        hint(f"Run: python3 {SCRIPT_DIR.name}/Apply.py")
        return False

    # 3. Server DLL exists (build was done)
    server_dll = BUILD_ROOT / "bin" / "Content.Server" / "Content.Server.dll"
    if not server_dll.exists():
        error("Content.Server.dll not found — build not done")
        hint(f"Run: python3 {SCRIPT_DIR.name}/deploy_full.py")
        return False

    # 4. Check Content.Packaging.dll is built
    packaging_dll = BUILD_ROOT / "Content.Packaging" / "bin" / "Debug" / "net10.0" / "Content.Packaging.dll"
    if not packaging_dll.exists():
        info("Content.Packaging not built — building now...")
        stdout, stderr, rc = run_cmd(
            f"dotnet build {PACKAGING_PROJECT} --no-restore",
            cwd=BUILD_ROOT, timeout=120
        )
        if rc != 0:
            error("Failed to build Content.Packaging")
            hint(f"stderr: {stderr[:300]}")
            return False
        ok("Content.Packaging built")

    return True


def check_runtime_restore(platform: str) -> bool:
    """Check if NuGet cache has runtime packages for target platform.

    Content.Packaging runs `dotnet publish --runtime <platform>` which needs
    Microsoft.NETCore.App.Runtime.<platform> in NuGet cache. In offline sandbox,
    this may not be available.
    """
    # Quick check: try restore with --runtime and see if it succeeds
    info(f"Checking NuGet restore for runtime: {platform}")
    stdout, stderr, rc = run_cmd(
        f"dotnet restore Content.Server/Content.Server.csproj --runtime {platform} --no-cache 2>&1 | tail -3",
        cwd=BUILD_ROOT, timeout=120
    )
    if rc != 0 or "error" in stderr.lower():
        warn(f"NuGet restore for {platform} may fail (offline cache limitation)")
        hint(f"If publish fails with 'Unable to resolve Microsoft.NETCore.App.Runtime.{platform}':")
        hint(f"  1. Run on a machine with internet: dotnet restore --runtime {platform}")
        hint(f"  2. Or pre-populate NuGet cache with runtime packages")
        hint(f"  3. Or use existing build: dotnet build (no publish, no runtime)")
        return False
    ok(f"Restore for {platform} available")
    return True


def publish_server(platform: str, skip_build: bool, hybrid_acz: bool,
                   configuration: str = "Release") -> bool:
    """Run Content.Packaging to create server release ZIP.

    Args:
        platform: target platform (linux-x64, linux-arm64, win-x64)
        skip_build: if True, use existing build (don't rebuild)
        hybrid_acz: use HybridACZ (smaller client download)
        configuration: Release, Debug, or Tools
    """
    args = ["server"]
    if skip_build:
        args.append("--skip-build")
    args.extend(["--platform", platform])
    if hybrid_acz:
        args.append("--hybrid-acz")
    args.extend(["--configuration", configuration])

    cmd_str = f"dotnet run --project {PACKAGING_PROJECT} --no-build -- " + " ".join(args)
    print(f"\n  Running: {cmd_str}")
    print(f"  Working dir: {BUILD_ROOT}")
    print(f"  This may take 5-15 minutes (build + publish + package Resources/)...")
    print()

    stdout, stderr, rc = run_cmd(cmd_str, cwd=BUILD_ROOT, timeout=1800)

    if rc != 0:
        error("Content.Packaging failed")
        print(f"\n--- stdout (last 30 lines) ---")
        for line in stdout.splitlines()[-30:]:
            print(f"  {line}")
        print(f"\n--- stderr (last 30 lines) ---")
        for line in stderr.splitlines()[-30:]:
            print(f"  {line}")
        # Detect common runtime package issue
        if "Microsoft.NETCore.App.Runtime" in stderr:
            print()
            error("This is the offline-sandbox runtime package limitation.")
            hint("The NuGet cache doesn't have runtime packages for this platform.")
            hint("Options:")
            hint("  1. Run publish_server.py on a machine with internet")
            hint("  2. Pre-populate NuGet cache: dotnet restore --runtime " + platform)
            hint("  3. For testing in sandbox: use deploy_full.py --build-only (no publish)")
        return False

    print("--- output (last 15 lines) ---")
    for line in stdout.splitlines()[-15:]:
        print(f"  {line}")

    return True


def verify_release(platform: str) -> bool:
    """Verify release/ contains the expected ZIP file."""
    zip_path = RELEASE_DIR / f"SS14.Server_{platform}.zip"
    if not zip_path.exists():
        error(f"Release ZIP not created: {zip_path}")
        # Check if release dir exists at all
        if RELEASE_DIR.exists():
            print(f"\n  Files in release/:")
            for f in sorted(RELEASE_DIR.iterdir()):
                print(f"    {f.name} ({f.stat().st_size // 1024} KB)")
        return False

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print()
    kv("Release ZIP", str(zip_path.relative_to(BUILD_ROOT)), indent=4)
    kv("Size", f"{size_mb:.1f} MB", indent=4)

    # Try to peek inside the ZIP to verify Resources/ is there
    import zipfile
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            total = len(names)
            resources_count = sum(1 for n in names if n.startswith("Resources/"))
            aavikko_check = any("Aavikko/" in n for n in names[:200])

            kv("Total entries", total, indent=4)
            kv("Resources entries", resources_count, indent=4)
            kv("Aavikko content", "FOUND" if aavikko_check else "NOT FOUND", indent=4)
    except Exception as e:
        warn(f"Could not inspect ZIP: {e}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Package SS14 server + content bundle into release/SS14.Server_<platform>.zip"
    )
    parser.add_argument("--platform", default="linux-x64",
                        choices=["linux-x64", "linux-arm64", "win-x64", "osx-x64", "osx-arm64"],
                        help="Target platform (default: linux-x64)")
    parser.add_argument("--no-skip-build", action="store_true",
                        help="Rebuild Content.Server before packaging (default: skip)")
    parser.add_argument("--hybrid-acz", action="store_true",
                        help="Use HybridACZ for smaller client download")
    parser.add_argument("--configuration", default="Release",
                        choices=["Release", "Debug", "Tools"],
                        help="Build configuration (default: Release)")
    parser.add_argument("--skip-restore-check", action="store_true",
                        help="Skip NuGet runtime restore check (faster, may fail later)")
    args = parser.parse_args()

    header("Aavikko Server Publisher",
           f"package server → release/SS14.Server_{args.platform}.zip")

    # 1. Check prerequisites
    section(1, 5, "Check prerequisites")
    if not check_prerequisites():
        sys.exit(1)
    ok("All prerequisites met")

    # 2. Show current state
    section(2, 5, "Current state")
    applied_info = {}
    try:
        import json
        applied_info = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    kv("Applied at", applied_info.get("applied_at", "?"), indent=4)
    kv("Head commit", applied_info.get("head_commit", "?")[:12], indent=4)
    kv("Patches copied", applied_info.get("patches_copied", 0), indent=4)
    kv("Mods copied", applied_info.get("mods_copied", 0), indent=4)
    kv("CS patches", len(applied_info.get("cs_patches_applied", [])), indent=4)

    # 3. Check runtime restore (NuGet cache has packages for this platform?)
    section(3, 5, f"Check NuGet runtime: {args.platform}")
    if not args.skip_restore_check:
        check_runtime_restore(args.platform)
    else:
        info("Skipping restore check (--skip-restore-check)")

    # 4. Run packaging
    section(4, 5, f"Package server ({args.platform})")
    t_start = time.time()
    success = publish_server(
        platform=args.platform,
        skip_build=not args.no_skip_build,
        hybrid_acz=args.hybrid_acz,
        configuration=args.configuration,
    )
    elapsed = time.time() - t_start

    if not success:
        fail_banner(
            f"Packaging failed after {elapsed:.1f}s",
            hints=[
                "Check the output above for errors.",
                "Common causes:",
                "  - NuGet cache missing runtime packages (offline sandbox limitation)",
                "  - Apply.py not run (Resources/ has no Aavikko content)",
                "  - Build not up-to-date (run deploy_full.py first)",
            ],
        )
        sys.exit(1)

    ok(f"Packaging completed in {elapsed:.1f}s")

    # 5. Verify release
    section(5, 5, "Verify release")
    if not verify_release(args.platform):
        sys.exit(1)

    # Done
    zip_path = RELEASE_DIR / f"SS14.Server_{args.platform}.zip"
    print()
    success_banner(
        f"Server published successfully",
        details=[
            ("Platform", args.platform),
            ("Release ZIP", str(zip_path.relative_to(BUILD_ROOT))),
            ("Configuration", args.configuration),
            ("Total time", f"{elapsed:.1f}s"),
        ],
        next_step=(
            f"To run server:\n"
            f"  unzip {zip_path.name} -d server/\n"
            f"  cd server/\n"
            f"  ./Content.Server --config-file server_config.toml\n"
            f"\n"
            f"Clients connect via SS14 launcher to <server-ip>:1212\n"
            f"Content bundle (Resources/) is transferred to clients automatically."
        ),
    )


if __name__ == "__main__":
    main()