# Aavikko Overlay — VS Code Extension

Интеграция Aavikko overlay-системы (SS14 modding) в VS Code: git-aware отслеживание изменений, бейджи на overlay-файлах, разрешение конфликтов, запуск Patcher-скриптов одним кликом.

## Что нового в 0.2.0

- **Status.py-мост** — единственный источник правды о состоянии (Python считает, расширение показывает). Без Status.py работает fallback на чистом JS.
- **Dirty Files через git** — `git status --porcelain` вместо сканирования по mtime: корректно после checkout/pull, видит untracked-файлы, поддерживает RobustToolbox.
- **Overview-панель** — состояние overlay, ветка/HEAD, счётчики патчей и модов, быстрые действия.
- **Безопасный запуск скриптов** — `execFile` без shell (нет проблем с кавычками/инъекциями), автодетект `python3 → python → py` (Windows-friendly).
- **Прогресс и логи** — операции Apply/Clear/Generate/Validate показывают прогресс, вывод падает в Output-канал «Aavikko».
- **Diff-превью на Windows** — `git apply` во временной папке вместо unix-утилиты `patch`.
- **Надёжная запись decisions** — `.conflict_decisions.yml` парсится в модель и пересериализуется целиком (v0.1 мог портить файл).
- **Настройки** — `aavikko.pythonPath`, `aavikko.highlightPatchedLines`, `aavikko.autoRefreshOnSave`, `aavikko.autoCheckConflicts`.

## Возможности

| Что | Как |
|-----|-----|
| Статус в строке состояния | `Aavikko: Applied (Np)` / `Pristine`; клик — меню действий |
| Бейджи в Explorer | синий = mod (новый файл), оранжевый = patch, фиолетовый = и то и другое |
| Подсветка в редакторе | строки, добавленные патчем, подсвечены оранжевым |
| Dirty Files | изменённые upstream-файлы, группировка Content / Resources / RobustToolbox |
| Conflicts | конфликты с upstream + 4 вида diff + решения (Force Replace / Updated / Skip / Ignore и т.д.) |
| Generate Patch | из контекстного меню редактора или из Dirty Files |

## Установка

```bash
code --install-extension aavikko-overlay-0.2.0.vsix
# или: Extensions view → «...» → Install from VSIX…
```

Расширение активируется автоматически, если в workspace есть `Aavikko.Modding/Patcher/Apply.py`.

## Сборка из исходников

```bash
cd aavikko-overlay
npm install
npm run compile     # tsc → out/
npx vsce package --no-dependencies
```