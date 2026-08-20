# Aavikko Modding

Overlay modding system for Space Station 14 builds. Apply custom resources, patch C# code, and add new functionality — without modifying upstream files.

## Structure

```
<SS14 build root>/
├── 00_Aavikko/
│   ├── 00_Modding/
│   │   ├── 00_Patcher/              ← Patcher scripts + state files
│   │   │   ├── x00_Apply.py          ← Apply overlay to upstream
│   │   │   ├── x01_Generate.py       ← Capture git diff → .cs.patch
│   │   │   ├── x02_Clear.py          ← Revert upstream to HEAD
│   │   │   ├── x03_Status.py         ← JSON status for VS Code extension
│   │   │   ├── x04_Check.py          ← Detect upstream conflicts
│   │   │   ├── x05_Validate.py       ← Validate overlay placement
│   │   │   ├── x06_Migrate.py        ← Generate overlay from diff (one-time)
│   │   │   ├── x07_DeployFull.py     ← Full deploy pipeline (AI/host orchestrator)
│   │   │   ├── x08_PublishServer.py   ← Publish server for remote launcher connections
│   │   │   ├── l00_common.py          ← Shared utilities (paths, subprocess, timing)
│   │   │   ├── l01_ui.py              ← Console UI helpers (colors, banners, progress)
│   │   │   ├── l02_symlinks.py        ← Navigation symlinks (@patched, @Path)
│   │   │   └── README.md              ← This file
│   │   └── 99_VSCode/
│   │       └── aavikko-overlay/       ← VS Code extension source
│   │
│   ├── 01_Resources/                  ← Resource overlay (copied to Resources/)
│   │   ├── Mods/                      ← New Aavikko-only files
│   │   └── Patches/                   ← Modified upstream resources (overwrite)
│   │
│   ├── 02_Content/                    ← C# code overlay
│   │   ├── Mods/                      ← New .cs files (SDK-style, auto-discovered)
│   │   └── Patches/                   ← .cs.patch / .xaml.patch (git apply)
│   │
│   └── 03_RobustToolbox/             ← Engine overlay (rarely used)
│       ├── Mods/
│       └── Patches/
│
├── Resources/                         ← Upstream SS14 resources (target of overlay)
├── Content.Server/                    ← Upstream SS14 server
├── Content.Client/                    ← Upstream SS14 client
├── Content.Shared/                    ← Upstream SS14 shared
└── RobustToolbox/                     ← Engine (submodule)
```

## Quick start

```bash
cd 00_Aavikko/00_Modding/00_Patcher/

# 1. Apply overlay (copies Mods/Patches → upstream, applies .cs.patch)
python3 x00_Apply.py

# 2. Build
dotnet build Content.Server --no-restore

# 3. When done — revert upstream to clean state
python3 x02_Clear.py
```

## How it works

### New files → `00_Aavikko/01_Resources/Mods/` or `00_Aavikko/02_Content/Mods/`

- Place new files here (sprites, audio, .cs, .xaml)
- `x00_Apply.py` copies them to upstream at the same relative path
- SDK-style csproj auto-discovers .cs files — no csproj patching needed
- `x02_Clear.py` removes them (via `git clean -fd`)

### Modified upstream files → `Mods/Patches/`

Two types:

**Resources** (YAML, PNG, OGG, FTL):
- Place in `01_Resources/Patches/<mirror_path>` (same path as in `Resources/`)
- `x00_Apply.py` copies the file, overwriting upstream

**C# / XAML** (`.cs`, `.xaml`):
- Place in `02_Content/Patches/<mirror_path>.cs.patch`
- `x00_Apply.py` applies via `git apply --batch` (one spawn for all patches)
- Cross-platform CRLF fix: `git -c core.autocrlf=false apply`

### Generating patches

```bash
# After editing an upstream file:
python3 x01_Generate.py Content.Shared/Botany/Systems/PlantSystem.cs

# Or capture ALL modified files at once:
python3 x01_Generate.py --all

# Or just list what's modified:
python3 x01_Generate.py --list
```

`x01_Generate.py` auto-detects path type:
- `Resources/...` → copied to `01_Resources/Patches/` (if tracked) or `01_Resources/Mods/` (if new)
- `Content.*/*.cs` → `.cs.patch` in `02_Content/Patches/`
- `RobustToolbox/...` → `.cs.patch` in `03_RobustToolbox/Patches/`

### Navigation symlinks

After Apply, `l02_symlinks.py` creates `@patched` symlinks next to each `.cs.patch` file, pointing to the patched `.cs` file in the build tree. This lets you quickly jump between patch and result in VS Code.

## Commands

| Command | What it does |
|---|---|
| `x00_Apply.py` | Apply overlay (copy Mods/Patches, apply .cs.patch, create symlinks) |
| `x00_Apply.py --force` | Skip conflict check, force apply |
| `x00_Apply.py --reapply` | Force re-apply even if already applied |
| `x01_Generate.py <path>` | Capture git diff for one file → .cs.patch or copy |
| `x01_Generate.py --all` | Capture all modified files |
| `x01_Generate.py --list` | List modified files |
| `x02_Clear.py` | Revert upstream to HEAD (git checkout + git clean) |
| `x02_Clear.py --dry-run` | Show what would be reverted |
| `x03_Status.py` | Pretty overlay status (state, git, counts, dirty files) |
| `x03_Status.py --json` | JSON output for VS Code extension |
| `x04_Check.py` | Detect upstream changes that conflict with overlay |
| `x04_Check.py --baseline` | Re-record baseline (forget old conflicts) |
| `x05_Validate.py` | Validate overlay placement (Mods vs Patches) |
| `x06_Migrate.py` | One-time: generate overlay from diff(old_build, new_build) |
| `x07_DeployFull.py` | Full deploy pipeline for sandbox (Clear→Migrate→Apply→Build) |
| `x08_PublishServer.py` | Publish server build for remote launcher connections |

## State files

All in `00_Patcher/` (auto-managed, don't edit manually):

| File | Purpose |
|---|---|
| `.applied` | JSON: applied timestamp, HEAD commit, patch counts |
| `.upstream_state.json` | Baseline SHA1 hashes of tracked upstream files |
| `.conflict_decisions.yml` | Developer's conflict resolution choices (fr/u/s/i) |
| `.symlinks.json` | Tracked symlinks (for fast removal) |
| `.apply.lock` | Prevents concurrent Apply.py runs |

## VS Code extension

Install `aavikko-overlay-0.3.1.vsix` from `99_VSCode/`:

```bash
code --install-extension 00_Aavikko/00_Modding/99_VSCode/aavikko-overlay-0.3.1.vsix --force
```

Features:
- Status bar: Applied/Pristine indicator with patch count
- Pristine warning: red pulsing banner when editing upstream without overlay
- Overview panel: git info, overlay counts, dirty files, conflicts
- Dirty Files panel: files modified after Apply, grouped by type
- Conflicts panel: inline resolution buttons (force replace / update / skip)
- Explorer badges: blue (mod), orange (patch), purple (both)
- One-click Apply/Clear/Generate/Check/Validate
- Periodic polling (15s) — catches state changes on network drives

## Performance

- **Batch git apply**: all 32 .cs.patch files applied in one `git apply` spawn (~0.2s vs 5s)
- **Batch git checkout**: Content.* reverted in one `git checkout HEAD --` call (~0.3s vs 1.7s)
- **git ls-tree for conflict check**: one `git ls-tree -r HEAD` instead of 5000 `git show` calls (~50ms vs 8min on Windows)
- **Parallel file copy**: ThreadPoolExecutor for shutil.copy (~2-4x speedup on SSD)
- **Cached mkdir**: directories created once per copy_tree run (not per file)
- **State file for symlinks**: `.symlinks.json` tracks created symlinks for O(N) removal

## Cross-platform

- **Linux/macOS**: native symlinks, fcntl locking
- **Windows**: msvcrt locking, `core.autocrlf=false` for git apply, UTF-8 stdout via `sys.stdout.reconfigure()`
- **PowerShell**: no `&&` in terminal commands (uses `createTerminal({cwd})` instead)
- **Python**: `-X utf8` flag for child processes (Windows cp1251 fix)
