# Aavikko Modding

Universal modding system for Space Station 14 builds. Overlay resources, patch C# code, and add new functionality — without modifying upstream files.

## Structure

```
<SS14 build root>/
├── 00_Aavikko/02_Content/          ← New C# files (.cs.mod)
│   ├── Content.Server/
│   ├── Content.Client/
│   └── Content.Shared/
│
├── 00_Aavikko/01_Resources/        ← Resource overlay (--mount-dir)
│   ├── Textures/
│   ├── Prototypes/
│   ├── Audio/
│   └── Locale/
│
└── 00_Aavikko/00_Modding/          ← Tools + patches
    ├── Patcher/
    │   ├── Generate.py       ← Scan git changes, create .cs.mod + .cs.patch
    │   ├── Apply.py          ← Apply patches, restore .cs.mod → .cs
    │   ├── Clear.py          ← Revert upstream to base_commit
    │   ├── Status.py         ← (new) JSON status bridge for the VS Code extension
    │   ├── .base_commit      ← (auto) Corvax + RobustToolbox HEAD hashes
    │   ├── .applied          ← (auto) List of applied patches
    │   └── Patches/
    │       ├── RobustToolbox/    ← Engine patches
    │       ├── Content.Server/   ← Server C# patches
    │       ├── Content.Client/   ← Client C# patches
    │       └── Content.Shared/   ← Shared C# patches
    │
    └── Launcher/
        ├── Linux/            ← .sh scripts
        └── Windows/          ← .bat scripts
```

## Quick start

```bash
# 1. Edit upstream code as usual (or create new .cs files)
vim Content.Server/Administration/Commands/MyCommand.cs
vim Content.Shared/Botany/Systems/PlantSystem.cs  # fix a bug

# 2. Generate mod files (scans git, creates .cs.mod + .cs.patch)
python3 00_Aavikko/00_Modding/Patcher/Generate.py

# 3. Build (Clear + Apply + dotnet build)
bash 00_Aavikko/00_Modding/Launcher/Linux/Server.RestoreAndBuild.sh

# 4. Run
bash 00_Aavikko/00_Modding/Launcher/Linux/Server.Run.sh

# 5. Commit (only Aavikko.* folders!)
git add 00_Aavikko/02_Content/ 00_Aavikko/00_Modding/ 00_Aavikko/01_Resources/
git commit -m "feat: my changes"
```

## How it works

### New C# code → `00_Aavikko/02_Content/` (`.cs.mod` files)

- Write `.cs` files anywhere in `Content.*/`
- `Generate.py` copies them to `00_Aavikko/02_Content/` as `.cs.mod`
- `Apply.py` restores them as `.cs` during build + adds `<Compile Include>` to `.csproj`

### Modified upstream C# → `00_Aavikko/00_Modding/Patches/` (`.cs.patch` files)

- Edit upstream `.cs` file
- `Generate.py` creates `git diff` as `.cs.patch`
- `Apply.py` applies patch via `git apply`

### Modified RobustToolbox → `00_Aavikko/00_Modding/Patches/RobustToolbox/`

- Same as above, but for `RobustToolbox/` submodule
- `Apply.py` handles paths correctly

### Resource overlay → `00_Aavikko/01_Resources/`

- Place textures, prototypes, audio, locale here
- `Server.Run.sh` passes `--mount-dir 00_Aavikko/01_Resources` to SS14
- RobustToolbox loads mod resources **before** upstream (mod wins)

## Conflict detection

`Apply.py` checks if upstream files changed since `.base_commit` was recorded.

If upstream changed:
```
[CONFLICT] 001-plant-system.cs.patch
  Target: Content.Shared/Botany/Systems/PlantSystem.cs
  Upstream file changed since base commit abc1234.

Options:
  [s] Skip this patch
  [f] Force apply (may fail)
  [a] Abort
```

## Commands

| Command | What it does |
|---|---|
| `Generate.py` | Scan git, create `.cs.mod` + `.cs.patch` (doesn't touch upstream) |
| `x01_Generate.py --clean` | Remove all generated files |
| `Apply.py` | Apply patches + restore `.cs.mod` (requires clean upstream) |
| `x00_Apply.py --force` | Skip conflict checks |
| `x00_Apply.py --skip-conflicts` | Skip conflicting patches |
| `Clear.py` | Revert upstream to base_commit |
| `x02_Clear.py --dry-run` | Show what would be cleared |
| `Status.py` | Pretty overlay status summary (state, git, overlay counts, dirty files, conflicts) |
| `Status.py --json` | Same as JSON — consumed by the VS Code extension (read-only, never crashes) |

## VS Code extension

`00_Aavikko/00_Modding/VSCode/aavikko-overlay-0.2.0.vsix` — install via
`code --install-extension` or Extensions view → «Install from VSIX…».

Features: status-bar indicator (click = action menu), Overview / Dirty Files /
Conflicts panels, Explorer badges (blue = mod, orange = patch), inline
highlight of patched lines, one-click Apply/Clear/Generate/Check, conflict
diffs and resolution. Source lives in `VSCode/aavikko-overlay/`.

## Launcher scripts

| Script | What it does |
|---|---|
| `Server.RestoreAndBuild.sh` | Clear + Apply + `dotnet build` |
| `Server.Run.sh` | `dotnet run` with `--mount-dir` |
| `Server.Clear.sh` | Clear patches |
| `Client.RestoreAndBuild.sh` | Same for client |
| `Client.Run.sh` | Same for client |

## Examples

See:
- `00_Aavikko/02_Content/Content.Server/Administration/Commands/HelloAavikkoCommand.cs.mod` — new command
- `00_Aavikko/00_Modding/Patches/Content.Shared/001-plant-system-try-get-tray.cs.patch` — bug fix
- `00_Aavikko/00_Modding/Patches/RobustToolbox/001-program-shared-mount-dir.cs.patch` — engine fix