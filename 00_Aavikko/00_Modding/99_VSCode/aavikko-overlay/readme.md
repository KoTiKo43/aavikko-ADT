# Aavikko Overlay — VS Code Extension

Интеграция Aavikko overlay-системы (SS14 modding) в VS Code: git-aware отслеживание изменений, бейджи на overlay-файлах, разрешение конфликтов, запуск Patcher-скриптов одним кликом.

**Текущая версия:** 0.3.7 — см. CHANGELOG.md (в корне расширения) для истории изменений.

## Возможности

| Что | Как |
|-----|-----|
| StatusBar | `Aavikko: Applied (Np · Mm)` / `Pristine`; клик — меню действий |
| Overview-панель | состояние overlay, ветка/HEAD, счётчики applied/uncaptured, быстрые действия |
| Бейджи в Explorer | синий = mod (новый файл), оранжевый = patch, фиолетовый = оба |
| Подсветка в редакторе | строки, добавленные `.cs.patch`, подсвечены оранжевым |
| Dirty Files | изменённые upstream-файлы, группировка Recent / Content / Resources / RobustToolbox |
| Conflicts | конфликты с upstream + 4 вида diff + решения (Force Replace / Updated / Skip / Ignore и т.д.) |
| Generate Patch | из контекстного меню редактора или из Dirty Files |
| Pristine-warning | пульсирующий красный баннер если редактируете upstream без Apply |

## Структура проекта

Расширение активируется автоматически, если в workspace есть маркер:
```
00_Aavikko/00_Modding/00_Patcher/x00_Apply.py
```

Overlay-файлы раскладываются по трём корням:
```
00_Aavikko/
├── 01_Resources/
│   ├── Mods/      ← новые файлы → копируются в Resources/
│   └── Patches/   ← изменённые upstream файлы → перезаписывают Resources/
├── 02_Content/
│   ├── Mods/      ← новые .cs файлы (SDK-style)
│   └── Patches/   ← .cs.patch / .xaml.patch (git apply)
└── 03_RobustToolbox/
    ├── Mods/
    └── Patches/
```

## Установка

```bash
code --install-extension aavikko-overlay-0.3.6.vsix
# или: Extensions view → «...» → Install from VSIX…
```

## Сборка из исходников

```bash
cd aavikko-overlay
npm install
npm run compile     # tsc → out/
npx vsce package --no-dependencies
```

## Настройки

| Setting | Default | Описание |
|---------|---------|----------|
| `aavikko.pythonPath` | `""` | Путь к Python (если пусто — автодетект `python3 → python → py`) |
| `aavikko.highlightPatchedLines` | `true` | Подсветка строк, добавленных патчами |
| `aavikko.autoRefreshOnSave` | `true` | Обновлять Dirty Files при сохранении |
| `aavikko.autoCheckConflicts` | `true` | Обновлять Conflicts при движении upstream HEAD |

## Смотрите также

- **CHANGELOG.md** (в корне расширения) — история всех версий
- **README.md в Patcher** (`00_Aavikko/00_Modding/00_Patcher/README.md`) — документация Python-скриптов
