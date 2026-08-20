#!/usr/bin/env python3
"""ui.py — console UI helpers for Aavikko modding scripts.

Features:
  - ANSI colors (auto-disabled in non-TTY / NO_COLOR env / Windows without colorama)
  - Progress bar with ETA
  - Section headers, banners, KV tables
  - All functions degrade to plain text when colors unavailable

Design:
  - No external dependencies (pure stdlib + optional colorama on Windows)
  - isatty() check: if stdout/stderr not a TTY, colors are stripped
  - NO_COLOR env var (https://no-color.org/) disables colors
  - Functions return strings for color helpers (green/red/etc) so they can be
    embedded in f-strings: print(f"{red('ERROR')}: something broke")
"""
from __future__ import annotations
import os
import sys
import time
from typing import Any, Iterable

# Force UTF-8 for stdout/stderr — Windows default is cp1251 which can't encode
# Unicode characters like →, —, ✓, ⚠ used in this module's output.
# Without this, scripts using ui.py crash with UnicodeEncodeError on Windows.
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        pass

WIDTH = 70

# ── Color detection ─────────────────────────────────────────────────────────

def _detect_color_support() -> bool:
    """Detect whether stdout supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    # Windows: check for ANSICON or Windows 10+ VT mode
    if sys.platform == "win32":
        if not os.environ.get("ANSICON"):
            # Windows 10+ supports VT, but only if enabled
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
                return True
            except Exception:
                return False
    return True


_COLORS_ENABLED = _detect_color_support()


# ANSI escape codes
class _ANSI:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    GREY    = "\033[90m"


def _wrap(text: str, color: str) -> str:
    """Wrap text in ANSI color codes if colors enabled, else return as-is."""
    if not _COLORS_ENABLED or not text:
        return text
    return f"{color}{text}{_ANSI.RESET}"


# ── Color helpers (return strings, can be embedded in f-strings) ────────────

def bold(text: str) -> str:
    return _wrap(text, _ANSI.BOLD)

def dim(text: str) -> str:
    return _wrap(text, _ANSI.DIM)

def green(text: str) -> str:
    return _wrap(text, _ANSI.GREEN)

def yellow(text: str) -> str:
    return _wrap(text, _ANSI.YELLOW)

def red(text: str) -> str:
    return _wrap(text, _ANSI.RED)

def cyan(text: str) -> str:
    return _wrap(text, _ANSI.CYAN)

def magenta(text: str) -> str:
    return _wrap(text, _ANSI.MAGENTA)


# ── Print helpers ───────────────────────────────────────────────────────────

def header(title: str, subtitle: str = "") -> None:
    """Print a prominent header with top/bottom rule."""
    bar = "=" * WIDTH
    print(f"{bold(bar)}")
    print(f"  {bold(title)}")
    if subtitle:
        print(f"  {dim(subtitle)}")
    print(f"{bold(bar)}")


def section(num, total, title: str) -> None:
    """Print a section header.
    num/total may be int (renders [N/M]) or any string (renders [N/M] verbatim).
    If both None, renders without bracket prefix."""
    if num is None and total is None:
        print(f"\n{bold('---')} {title} {bold('---')}")
    else:
        prefix = f"{dim('[')}{cyan(str(num))}/{cyan(str(total))}{dim(']')}"
        print(f"\n{bold('---')} {prefix} {title} {bold('---')}")


def divider() -> None:
    print(dim("-" * WIDTH))


def kv(key: str, value: Any, indent: int = 2) -> None:
    print(f"{' ' * indent}{bold(key)}: {value}")


def ok(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}{bold(green('[OK]'))} {msg}")


def info(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}{bold(cyan('[INFO]'))} {msg}")


def warn(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}{bold(yellow('[WARN]'))} {msg}", file=sys.stderr)


def error(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}{bold(red('[ERROR]'))} {msg}", file=sys.stderr)


def fatal(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}{bold(red('[FATAL]'))} {msg}", file=sys.stderr)


def skip(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}{bold(dim('[SKIP]'))} {msg}")


def hint(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}  {dim('→')} {msg}")


def tag(label: str, msg: str, indent: int = 0, color=None) -> None:
    """Print a tagged message. `color` can be a color function (green/red/cyan/etc.)
    or an ANSI code string. If None, uses bold."""
    if color is None:
        label_str = bold(label)
    elif callable(color):
        # color is a function like green/red/cyan — call it on the label
        label_str = color(label)
    else:
        # color is an ANSI code string
        label_str = _wrap(label, color)
    print(f"{' ' * indent}{dim('[')}{label_str}{dim(']')} {msg}")


def bullet(msg: str, indent: int = 0) -> None:
    print(f"{' ' * indent}  {cyan('•')} {msg}")


# ── Progress ────────────────────────────────────────────────────────────────


def progress_iter(items: list, label: str = "", unit: str = "",
                  update_every: int = 100) -> Iterable:
    """Iterate with progress messages every N items.

    Prints: '  Label: 100/5000 unit' every update_every items.
    Uses \r for in-place update when TTY, otherwise prints newlines.
    """
    total = len(items)
    use_carriage = _COLORS_ENABLED  # TTY → can use \r
    for i, item in enumerate(items, 1):
        if i % update_every == 0 or i == total:
            pct = (i * 100) // total if total else 0
            line = f"  {label}: {dim(f'{i}/{total}')} {unit} {dim(f'({pct}%)')}"
            if use_carriage:
                sys.stdout.write(f"\r{line:<60}")
                sys.stdout.flush()
                if i == total:
                    sys.stdout.write("\n")
            else:
                print(line)
        yield item


# ── Banners & tables ────────────────────────────────────────────────────────


def summary_table(rows: list[tuple[str, str]]) -> None:
    """Print a 2-column table with aligned keys."""
    if not rows:
        return
    max_key = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {bold(key.ljust(max_key))}  {value}")


def success_banner(title: str, details: list[tuple[str, str]] | None = None,
                   next_step: str = "") -> None:
    """Print a success banner with optional details and next-step hint."""
    bar = bold(green("=" * WIDTH))
    print(f"\n{bar}")
    print(f"  {bold(green('✓'))} {bold(title)}")
    if details:
        max_key = max(len(k) for k, _ in details) if details else 0
        for key, value in details:
            print(f"  {bold(key.ljust(max_key))}  {value}")
    if next_step:
        print(f"\n  {bold('Next:')} {dim(next_step)}")
    print(f"{bar}")


def fail_banner(title: str, hints: list[str] | None = None) -> None:
    """Print a failure banner with optional hint lines."""
    bar = bold(red("=" * WIDTH))
    print(f"\n{bar}", file=sys.stderr)
    print(f"  {bold(red('✗'))} {bold(title)}", file=sys.stderr)
    if hints:
        for h in hints:
            print(f"  {dim('→')} {h}", file=sys.stderr)
    print(f"{bar}", file=sys.stderr)
