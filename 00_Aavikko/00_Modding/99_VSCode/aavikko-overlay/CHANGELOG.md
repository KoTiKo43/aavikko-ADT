# Changelog — Aavikko Overlay (VS Code Extension)

Все заметные изменения расширения задокументированы здесь.
Формат основан на [Keep a Changelog](https://keepachangelog.com/), версияруется [SemVer](https://semver.org/).

## [0.3.6] — 2026-08-21

### Исправлено
- **StatusBar показывал `0p` для ADT-overlay.** Старый код считал только `cs_patches_applied` и `robust_patches_applied` (которые в ADT-overlay пустые — весь overlay там Resources-copies). Теперь StatusBar суммирует ВСЕ применённые файлы: `cs + robust + resources patches_copied`, плюс mods с разбивкой по типу.
- **Overview показывал `0 patches`.** Та же проблема — теперь показывает `203 patches · 363 mods` (для ADT-overlay).
- **Tooltip StatusBar** теперь показывает разбивку: `Patches: 0 cs + 0 robust + 203 resources` и `Mods: 363 resources + 0 content + 0 robust`.

## [0.3.5] — 2026-08-21

### Добавлено
- **Overview: разделение "Applied (tracked)" и "Uncaptured changes".** Раньше Overview показывал `Uncaptured changes 566` сразу после Apply — вводит в заблуждение (все 566 — это overlay-tracked файлы, применённые к upstream, а не новые uncaptured). Теперь две строки: "Applied (tracked) 566" (норма после Apply) и "Uncaptured changes N" (только `has_overlay=false`).

### Исправлено
- **StatusBar показывал `566 dirty` после Apply.** `dirty` теперь считает только `has_overlay=false` (т.е. реальные uncaptured изменения). Overlay-tracked файлы в подсчёте не участвуют — они и должны быть `M`/`??` после Apply.
- **Patcher `x03_Status.py` и `x01_Generate.py`:** фильтр `is_dirty_candidate()` теперь пропускает не только старый префикс `Aavikko.*`, но и новый `00_Aavikko/`. Раньше любой незакоммиченный файл внутри `00_Aavikko/` (например, новый `.ts`, `.vsix`, source改动) попадал в dirty-list. Теперь 0 таких утечек.

## [0.3.4] — 2026-08-21

### Исправлено
- **"Recent Changes" в Dirty Files показывал 500+ tracked файлов сразу после Apply.** Причина: код сравнивал `mtime(Resources) > applyTime`, а Apply.py копирует файлы и устанавливает их mtime ≈ applyTime. Теперь `has_overlay=true && mtime > applyTime + 5s` (5-секундный tolerance window) — реальное редактирование пользователем, а не артефакт Apply. Truly uncaptured (`!has_overlay`) всегда идут в Recent.

## [0.3.3] — 2026-08-21

### Исправлено
- **Команды `Check.py` и `Status.py` не запускались в ADT-overlay** — расширение вызывало старые имена файлов (`python3 Check.py`, `python3 Status.py`), которых в новой структуре Patcher нет. Теперь корректно вызывает `x04_Check.py` и `x03_Status.py`.
- **Текст в UI** (тултипы, viewsWelcome, readme) обновлён на новые имена скриптов: `Apply.py` → `x00_Apply.py`, `Clear.py` → `x02_Clear.py`, `Generate.py` → `x01_Generate.py`, `Validate.py` → `x05_Validate.py`.

## [0.3.2] — 2026-08-20

### Добавлено
- **Поддержка новой структуры `00_Aavikko/`.** Extension теперь активируется по маркеру `00_Aavikko/00_Modding/00_Patcher/x00_Apply.py` (вместо старого `Aavikko.Modding/Patcher/Apply.py`). Все overlay-пути (`Aavikko.Resources`, `Aavikko.Content`, `Aavikko.RobustToolbox`) заменены на `00_Aavikko/01_Resources`, `00_Aavikko/02_Content`, `00_Aavikko/03_RobustToolbox`.
- **x09_ShowcaseMap.py** — новый генератор карты-витрины всех Aavikko prototype IDs (вызывается из Apply.py).

### Изменено
- **Patcher scripts переименованы:** `Apply.py` → `x00_Apply.py`, `Generate.py` → `x01_Generate.py`, `Clear.py` → `x02_Clear.py`, `Status.py` → `x03_Status.py`, `Check.py` → `x04_Check.py`, `Validate.py` → `x05_Validate.py`, `Migrate.py` → `x06_Migrate.py`, `deploy_full.py` → `x07_DeployFull.py`, `publish_server.py` → `x08_PublishServer.py`.
- **Library modules переименованы:** `ui.py` → `l01_ui.py`, `SymLinks.py` → `l02_symlinks.py`, `common.py` → `l00_common.py`.

## [0.3.1] — 2026-08-19

### Добавлено
- **Группировка в Dirty Files:** "Recent Changes" / "Content" / "Resources" / "RobustToolbox". "Recent Changes" развёрнут по умолчанию (actionable), остальные свёрнуты (bulk tracked files).

## [0.3.0] — 2026-08-18

### Добавлено
- **Свой Activity Bar container** с SVG-иконкой Aavikko. Tree views переехали из Explorer в отдельную панель.

## [0.2.9] — 2026-08-17

### Исправлено
- Polling `setInterval` теперь ставится на паузу во время Apply/Clear/Generate (через `withBusy()`), иначе Status.py запускался каждые 15 секунд параллельно с долгой операцией и захлёбывался.

## [0.2.8] — 2026-08-15

### Добавлено
- **Polling fallback** каждые 15 секунд — catch state changes, которые FileSystemWatcher может пропустить на network-mounted drives (Crucible, NFS, FUSE, SMB/CIFS).
- **`state.ts` пропускает `00_Aavikko/`** в fallbackDirty — раньше overlay-исходники попадали в "Recent Changes".

## [0.2.7] — 2026-08-14

### Исправлено
- **Cross-platform `runScriptInTerminal`**: вместо `cd "DIR" && python3 ...` используем `createTerminal({cwd})` — работает в PowerShell, bash, zsh, cmd без shell-специфичных операторов.

## [0.2.6] — 2026-08-13

### Исправлено
- **CRLF-фикс для `git apply`**: добавлено `-c core.autocrlf=false`, чтобы патчи с LF корректно применялись на Windows с autocrlf=true.
- **`python3` → `sys.executable` + `-X utf8`**: Windows cp1251 fix — без `-X utf8` Python крашился с `UnicodeEncodeError` на русских буквах в путях.

## [0.2.5] — 2026-08-12

### Добавлено
- **Highlight patched lines в редакторе:** строки, добавленные `.cs.patch`, подсвечиваются оранжевым. Cache на filePath, инвалидация на save.

## [0.2.4] — 2026-08-11

### Исправлено
- **`run_git_with_lock_retry()`** — git `index.lock` race condition: если Apply.py и VS Code extension одновременно обращаются к git, один из них получает `index.lock exists`. Теперь retry 3 раза с backoff.

## [0.2.3] — 2026-08-10

### Добавлено
- **`_copy_with_retry()`** — Windows `PermissionError` при копировании файлов (антивирус/файловый сервер держит handle). Retry 5 раз с backoff 0.5s.

## [0.2.2] — 2026-08-09

### Добавлено
- **Pristine Upstream Warning:** отдельный right-aligned status bar item с красным фоном, виден когда overlay НЕ applied и активный редактор — upstream файл. Pulsing animation (3 фазы с разным padding).
- **Тултип** с подробной инструкцией: "1. Run Apply.py first, 2. Edit the file, 3. Run Generate.py".

## [0.2.1] — 2026-08-08

### Исправлено
- **`shell=True` → argv list** для всех `child_process` вызовов. `shlex.split` ломал Windows paths с пробелами.
- **MIME-тип multipart upload** явно `application/octet-stream` для бинарных файлов (PNG и т.д.).

## [0.2.0] — 2026-08-05

### Добавлено
- **x03_Status.py-мост** — единственный источник правды о состоянии. Python считает, расширение показывает. Без x03_Status.py работает fallback на чистом JS.
- **Dirty Files через `git status --porcelain`** вместо сканирования по mtime: корректно после checkout/pull, видит untracked-файлы, поддерживает RobustToolbox.
- **Overview-панель:** состояние overlay, ветка/HEAD, счётчики патчей и модов, быстрые действия.
- **Безопасный запуск скриптов:** `execFile` без shell (нет проблем с кавычками/инъекциями), автодетект `python3 → python → py` (Windows-friendly).
- **Прогресс и логи:** операции Apply/Clear/Generate/Validate показывают прогресс, вывод падает в Output-канал «Aavikko».
- **Diff-превью на Windows:** `git apply` во временной папке вместо unix-утилиты `patch`.
- **Надёжная запись decisions:** `.conflict_decisions.yml` парсится в модель и пересериализуется целиком (v0.1 мог портить файл).
- **Настройки:** `aavikko.pythonPath`, `aavikko.highlightPatchedLines`, `aavikko.autoRefreshOnSave`, `aavikko.autoCheckConflicts`.

## [0.1.0] — 2026-08-01

### Добавлено
- Первая версия расширения. Single-file `extension.ts` (~1400 строк).
- **Бейджи в Explorer:** синий = mod (новый файл), оранжевый = patch, фиолетовый = оба.
- **Dirty Files tree view** (mtime-based, не через git).
- **Conflict Resolver** с 4 видами diff (Force Replace / Updated / Skip / Ignore + Keep / Remove / Convert / Ignore) и double-click confirm.
- **StatusBar** с toggle Apply/Clear.
- **Запуск Patcher scripts** через интегрированный терминал.

---

## Технический debt / TODO

- [ ] Реальное определение "пользователь отредактировал tracked файл после Apply" — сейчас используем mtime > applyTime + 5s, но это эвристика. Лучше — SHA-сравнение с overlay source.
- [ ] Контекстное меню для "Generate Patch + Restore" уже есть, но не имеет shortcut.
- [ ] Sub-sec mtime precision на network drives может ломать `mtimeSafe` сортировку.
