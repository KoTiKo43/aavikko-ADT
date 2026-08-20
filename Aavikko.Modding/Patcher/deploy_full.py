#!/usr/bin/env python3
"""
deploy_full.py — full Aavikko deployment pipeline (host-side orchestrator).

Runs from the HOST (outside the sandbox container), orchestrating:
  1. Stop server + client (sandbox API)
  2. Clear.py (via /operations/run_command inside container)
  3. Migrate.py --clean (via /operations/run_command)
  4. Apply.py (via /operations/run_command)
  5. deploy_patch (sandbox API — applies Sdl3, AI API, sandbox-reflection)
  6. Wait for builds to complete
  7. Re-restore DB migrations (in case deploy_patch reverted them)

Solves the deploy_patch problem:
  - Clear.py reverts Content.* (including DB migrations) via git checkout
  - deploy_patch triggers build, which fails without migrations
  - This wrapper ensures: Clear → Migrate (copies migrations) → Apply → deploy_patch
  - After deploy_patch, re-runs Migrate --skip-conflicts to restore migrations if needed

Usage:
  python3 deploy_full.py [--skip-clear] [--skip-migrate] [--skip-apply]
                         [--skip-deploy-patch] [--skip-build] [--build-only]

Environment variables:
  SANDBOX_API    Sandbox API URL (default: http://scaledteam.ru:51335)
  SANDBOX_TOKEN  API token
  SANDBOX_BUILD  Build name (default: Corvax_Clean)
  BUILD_DIR      Build dir inside container (default: auto from build name)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Make sibling ui.py importable when run from any working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ui import (
    header, section, kv, ok, info, warn, error, hint, tag,
    summary_table, success_banner, dim, bold,
    green, yellow, red, cyan,
)

API = os.environ.get("SANDBOX_API", "http://scaledteam.ru:51335")
TOKEN = os.environ.get("SANDBOX_TOKEN", "")
BUILD = os.environ.get("SANDBOX_BUILD", "Corvax_Clean")
# Path inside container — relative to script location
# Script lives at: <build_root>/Aavikko.Modding/Patcher/deploy_full.py
# That's 3 levels deep: parent³ = build_root
_SCRIPT_PATH = os.path.dirname(os.path.abspath(__file__))  # .../Patcher/
_BUILD_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_PATH)))  # .../Corvax_Clean/
BUILD_DIR = os.environ.get("BUILD_DIR", _BUILD_ROOT)
PATCHER_DIR = f"{BUILD_DIR}/Aavikko.Modding/Patcher"


def api_call(method: str, path: str, params: dict | None = None,
             data: dict | None = None, timeout: int = 60) -> dict:
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"X-API-Token": TOKEN}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode(), "code": e.code}


def run_in_container(cmd: str, timeout: int = 300, poll_interval: float = 5.0) -> dict:
    """Run a command inside the sandbox container via /operations/run_command.
    Polls until completion. Returns {stdout, stderr, exit_code}."""
    # Submit command
    body = {"cmd": ["bash", "-lc", cmd], "timeout_seconds": timeout}
    r = api_call("POST", "/operations/run_command", data=body, timeout=30)
    op_id = r.get("op_id")
    if not op_id:
        return r

    # Poll for completion
    deadline = time.monotonic() + timeout + 30
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        result = api_call("GET", f"/operations/{op_id}", timeout=30)
        status = result.get("status")
        if status == "completed":
            return {
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "exit_code": result.get("exit_code"),
            }
        if status in ("failed", "cancelled"):
            return {
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "exit_code": result.get("exit_code", -1),
            }
    return {"error": "poll_timeout", "op_id": op_id}


def wait_build_done(target: str, timeout: int = 300) -> bool:
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        time.sleep(8)
        r = api_call("GET", "/ss14/build/status", params={"build": BUILD})
        bs = r.get("build_results", {}).get(target, {})
        status = bs.get("status", "unknown")
        if status != last_status:
            # Only print when status changes (avoid spamming identical lines)
            sys.stdout.write(f"\r  {target} build: {status}...{' ' * 10}")
            sys.stdout.flush()
            last_status = status
        if status in ("completed", "failed"):
            exit_code = bs.get("exit_code")
            # Clear the spinner line and print final result
            sys.stdout.write(f"\r{' ' * 60}\r")
            sys.stdout.flush()
            if status == "completed":
                ok(f"{target} build: {bold(green(status))} (exit={exit_code})")
            else:
                error(f"{target} build: {bold(red(status))} (exit={exit_code})")
            return status == "completed"
    # Clear spinner on timeout
    sys.stdout.write(f"\r{' ' * 60}\r")
    sys.stdout.flush()
    error(f"{target} build: TIMEOUT")
    return False


def check_notifications() -> bool:
    """Returns True if blocked by notifications."""
    n = api_call("GET", "/notifications")
    unread = n.get("unread", 0)
    if unread > 0:
        print()
        warn(f"{unread} unread notifications!")
        for notif in n.get("notifications", []):
            severity = notif['severity']
            color = {"error": red, "warning": yellow, "info": cyan}.get(severity, dim)
            print(f"  {color(f'[{severity.upper()}]')} {notif['title']}")
            # Read it
            nid = urllib.parse.quote(notif["id"], safe="")
            r = api_call("POST", f"/notifications/{nid}/read")
            content = r.get("content", "")
            if content:
                print(f"    {dim(content[:300])}")
        return True
    return False


def _print_container_output(r: dict, tail_lines: int = 10) -> None:
    """Pretty-print the tail of container command output.

    Container-side scripts (Clear.py / Migrate.py / Apply.py) already emit
    colored ui.py output, but since stdout is captured via API (not a TTY),
    colors are stripped automatically by the ui module's isatty() check.
    We just print the last N lines for context.
    """
    if r.get("stdout"):
        lines = r["stdout"].rstrip().split("\n")
        for line in lines[-tail_lines:]:
            print(f"  {line}")
    if r.get("stderr"):
        stderr_lines = r["stderr"].rstrip().split("\n")
        if stderr_lines and stderr_lines != [""]:
            print(f"  {dim('--- stderr (last 5 lines) ---')}", file=sys.stderr)
            for line in stderr_lines[-5:]:
                print(f"  {line}", file=sys.stderr)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Full Aavikko deployment pipeline")
    parser.add_argument("--skip-clear", action="store_true")
    parser.add_argument("--skip-migrate", action="store_true")
    parser.add_argument("--skip-apply", action="store_true")
    parser.add_argument("--skip-deploy-patch", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--build-only", action="store_true",
                        help="Only run dotnet build (skip everything else)")
    args = parser.parse_args()

    if not TOKEN:
        print("[FATAL] SANDBOX_TOKEN env var not set", file=sys.stderr)
        sys.exit(2)

    if args.build_only:
        args.skip_clear = True
        args.skip_migrate = True
        args.skip_apply = True
        args.skip_deploy_patch = True

    header("Aavikko Full Deployment Pipeline", "host-side orchestrator")
    kv("Build",      BUILD)
    kv("API",        API)
    kv("Build dir",  BUILD_DIR)
    kv("Patcher",    PATCHER_DIR)

    # Check notifications
    check_notifications()

    # ── Step 0: Stop server + client ──
    section(0, 7, "Stop server + client")
    r = api_call("POST", "/ss14/stop", params={"build": BUILD, "target": "both"})
    sr = r.get("stop_results", {})
    server_status = sr.get('server', {}).get('status', 'unknown')
    client_status = sr.get('client', {}).get('status', 'unknown')
    ok(f"server: {server_status}, client: {client_status}")
    time.sleep(2)

    # ── Step 1: Clear ──
    if not args.skip_clear:
        section(1, 7, "Clear (revert upstream)")
        r = run_in_container(f"cd {PATCHER_DIR} && python3 Clear.py", timeout=60)
        tag("exit_code", r.get('exit_code'), indent=2, color=cyan)
        _print_container_output(r, tail_lines=5)
        if r.get("exit_code", -1) != 0:
            error("Clear.py failed")
            hint("See container output above for details.")
            sys.exit(1)
    else:
        section(1, 7, "Clear (SKIPPED)")

    # ── Step 2: Migrate ──
    if not args.skip_migrate:
        section(2, 7, "Migrate (generate Mods/Patches + copy migrations)")
        r = run_in_container(f"cd {PATCHER_DIR} && python3 Migrate.py --clean", timeout=120)
        tag("exit_code", r.get('exit_code'), indent=2, color=cyan)
        _print_container_output(r, tail_lines=10)
        if r.get("exit_code", -1) != 0:
            error("Migrate.py failed")
            hint("See container output above for details.")
            sys.exit(1)
    else:
        section(2, 7, "Migrate (SKIPPED)")

    # ── Step 3: Apply ──
    if not args.skip_apply:
        section(3, 7, "Validate patches (pre-Apply sanity check)")
        r = run_in_container(f"cd {PATCHER_DIR} && python3 Validate.py", timeout=60)
        tag("exit_code", r.get('exit_code'), indent=2, color=cyan)
        _print_container_output(r, tail_lines=15)
        if r.get("exit_code", -1) != 0:
            error("Validate.py failed — refusing to Apply")
            hint("One or more .cs.patch / .xaml.patch files are corrupt.")
            hint("Fix them first, then re-run deploy_full.py.")
            hint("Auto-fix attempt: python3 Validate.py --fix")
            sys.exit(1)
        ok("All patches valid")

        section(4, 7, "Apply (overlay to Resources/ + Content.*)")
        r = run_in_container(f"cd {PATCHER_DIR} && python3 Apply.py", timeout=120)
        tag("exit_code", r.get('exit_code'), indent=2, color=cyan)
        _print_container_output(r, tail_lines=8)
        if r.get("exit_code", -1) != 0:
            error("Apply.py failed")
            hint("Likely cause: a patch failed to apply. Check stderr above.")
            hint("Fix: regenerate the failing patch with Generate.py --restore")
            sys.exit(1)
    else:
        section(4, 7, "Apply (SKIPPED)")

    # ── Step 5: deploy_patch ──
    if not args.skip_deploy_patch:
        section(5, 7, "deploy_patch (sandbox patches: Sdl3, AI API)")
        r = api_call("POST", "/ss14/deploy_patch", params={"build": BUILD}, timeout=120)
        applied = r.get('applied', [])
        skipped = r.get('skipped', [])
        if applied:
            ok(f"applied: {applied}")
        if skipped:
            info(f"skipped: {skipped}")

        # Wait for builds triggered by deploy_patch
        if r.get("build_triggered"):
            print()
            info("Waiting for builds...")
            wait_build_done("server", timeout=300)
            wait_build_done("client", timeout=300)

        # Re-restore migrations (deploy_patch build may have reverted them via git checkout)
        print()
        info("Re-restoring DB migrations...")
        r = run_in_container(
            f"cd {PATCHER_DIR} && python3 Migrate.py --only-migrations",
            timeout=60
        )
        if r.get("stdout"):
            lines = r["stdout"].strip().split("\n")
            for line in lines[-3:]:
                if "MIGRATION" in line or "Copied" in line:
                    print(f"  {line}")
    else:
        section(5, 7, "deploy_patch (SKIPPED)")

    # ── Step 6: Build verification ──
    if not args.skip_build:
        if not args.skip_deploy_patch:
            # deploy_patch already built — just verify
            section(6, 7, "Build verification")
            r = api_call("GET", "/ss14/build/status", params={"build": BUILD})
            for t in ("server", "client"):
                bs = r.get("build_results", {}).get(t, {})
                status = bs.get('status', 'unknown')
                exit_code = bs.get('exit_code')
                color = green if status == "completed" else (red if status == "failed" else yellow)
                print(f"  {t}: {color(status)} (exit={exit_code})")
        else:
            # Need to build manually
            section(6, 7, "Build (dotnet)")
            r = run_in_container(
                f"cd {BUILD_DIR} && set -o pipefail && dotnet build Content.Server --no-restore 2>&1 | tail -20",
                timeout=300
            )
            tag("server exit", r.get('exit_code'), indent=2, color=cyan)
            r = run_in_container(
                f"cd {BUILD_DIR} && set -o pipefail && dotnet build Content.Client --no-restore 2>&1 | tail -20",
                timeout=300
            )
            tag("client exit", r.get('exit_code'), indent=2, color=cyan)
    else:
        section(6, 7, "Build (SKIPPED)")

    # ── Step 7: Summary ──
    section(7, 7, "Summary")
    r = api_call("GET", "/ss14/status", params={"build": BUILD})
    server = r.get("server", {})
    client = r.get("client", {})
    summary_table([
        ("Server DLL", f"{server.get('dll_exists')} (built: {server.get('dll_built_at')})"),
        ("Client DLL", f"{client.get('dll_exists')} (built: {client.get('dll_built_at')})"),
    ])

    # Check notifications again
    check_notifications()

    success_banner(
        "Pipeline complete",
        next_step=(
            f"Start server: POST /ss14/start?build={BUILD}&target=server&wait_ready=true\n"
            f"  Start client: POST /ss14/start?build={BUILD}&target=client"
        ),
    )


if __name__ == "__main__":
    main()
