# ModSync

[![Тесты](https://github.com/zarepovka/modsync/actions/workflows/tests.yml/badge.svg)](https://github.com/zarepovka/modsync/actions/workflows/tests.yml)

> **Статус: v0.7.0 — Uninstall & Enable/Disable**

ModSync — небольшой кроссплатформенный менеджер модпаков с интерфейсом командной строки. Передайте друзьям файл `modpack.json`, и ModSync скачает включённые моды, проверит их, безопасно обновит установку и сохранит локальное состояние. Профили позволяют вести несколько наборов модов с независимыми state и backup.

## Возможности

- Установка ZIP-архивов и обычных файлов по HTTP(S)-ссылкам.
- Получение опубликованных стабильных релизов и assets через официальный GitHub API.
- Установка пакетов Thunderstore с выбором `latest` или точной версии.
- Рекурсивное разрешение, дедупликация и установка Thunderstore-зависимостей в правильном порядке.
- Расширяемый реестр источников: установщик не зависит от конкретного провайдера.
- Расширяемый реестр игровых адаптеров и первая реализация для Valheim + BepInEx.
- Полный план установки, file ownership и обнаружение конфликтов до первой записи.
- Безопасный dry-run без изменения игры, state или backup.
- Безопасные uninstall и временное enable/disable на основе file ownership.
- Потоковая загрузка с отображением прогресса без помещения всего файла в память.
- Проверка необязательной контрольной суммы SHA256 перед установкой.
- Безопасная распаковка ZIP с защитой от обхода путей и символических ссылок.
- Локальное хранение версий и SHA256 каждого установленного файла.
- Восстановление отсутствующих или повреждённых модов и обновление только изменившихся модов.
- Автоматическое резервное копирование перед обновлением и откат при ошибке.
- Проверка целостности backup и ручное восстановление предыдущего состояния.
- Несколько профилей с независимыми modpack, state и backup.
- Активный профиль для коротких команд без `--profile`.
- Кроссплатформенная блокировка изменяющих операций одного профиля.
- Понятные сообщения об ошибках без traceback для обычного пользователя.

## Требования

- Python 3.12 или новее;
- Windows, macOS или Linux;
- доступ к URL-адресам, указанным в модпаке.

## Установка

Клонируйте или скачайте репозиторий, затем создайте изолированное окружение:

```bash
cd modsync
python -m venv .venv
```

Активируйте его в macOS или Linux:

```bash
source .venv/bin/activate
```

Или в Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Установите ModSync:

```bash
python -m pip install .
```

Для разработки и запуска тестов используйте `python -m pip install ".[dev]"`. После изменения исходного кода переустановите пакет перед проверкой сгенерированной команды `modsync`.

## Использование

```bash
modsync profile create friends-server modpack.json
modsync profile activate friends-server
modsync profile list
modsync profile info friends-server
modsync install --profile friends-server
modsync install --profile friends-server --dry-run
modsync disable ExampleMod --profile friends-server
modsync enable ExampleMod --profile friends-server
modsync uninstall ExampleMod --profile friends-server
modsync verify --profile friends-server
modsync update --profile friends-server
modsync info --profile friends-server
modsync backup list --profile friends-server
modsync backup restore --profile friends-server <backup-id>
modsync profile delete friends-server
```

После `profile activate` имя можно не указывать:

```bash
modsync install
modsync update
modsync verify
modsync info
```

Прежний режим с явным файлом полностью сохранён:

```bash
modsync install modpack.json
modsync verify modpack.json
modsync update modpack.json
modsync info modpack.json
modsync backup list modpack.json
modsync backup restore modpack.json <backup-id>
```

Команды `install` и `update` идемпотентны: если версия мода и его установленные файлы уже соответствуют модпаку, мод будет пропущен. Отсутствующие, изменённые или повреждённые моды загружаются повторно. Отключённые моды остаются нетронутыми.

## Game Adapters

Source Provider и Game Adapter решают разные задачи:

- **Source Provider** определяет, откуда получить package: Direct URL, GitHub Releases или Thunderstore;
- **Game Adapter** определяет, как содержимое уже скачанного package установить для конкретной игры.

Новый pipeline не содержит игровых условных веток внутри installer:

```text
Modpack
   ↓
SourceRegistry
   ↓
Resolved packages
   ↓
GameRegistry
   ↓
GameAdapter
   ↓
InstallationPlan
   ↓
Backup
   ↓
Installer
   ↓
Verifier
```

Adapter-mode включается нормализованным идентификатором игры. В v0.6.0 поддерживается `"game": "valheim"`. Значения старых modpack, включая отображаемое `"Valheim"`, остаются в legacy-режиме: их `install_directory` по-прежнему означает непосредственную destination-папку. Это сохраняет совместимость с v0.5.0 без обязательной миграции.

## Valheim

В adapter-mode `install_directory` — корень игры, где находится `valheim.exe`, `valheim.x86_64`, `valheim.app` или `valheim_Data`, а не `BepInEx/plugins`. Автоматический поиск Steam library пока не выполняется.

ValheimAdapter проверяет признаки установки игры и ожидаемую структуру `BepInEx`. BepInEx автоматически не скачивается. Если сам BepInEx присутствует в разрешённом плане как явный package/dependency, проверка не выдаёт ложную ошибку до его установки.

## BepInEx Installation

Поддерживаются package-пути `plugins`, `core`, `patchers`, `monomod`, `config` и эквивалентные пути с одним префиксом `BepInEx/`. Двойной путь `BepInEx/BepInEx/...` отклоняется.

Для `plugins`, `core`, `patchers` и `monomod` файлы изолируются в `Author-Package` для Thunderstore или в безопасном имени мода для Direct/GitHub. `config` устанавливается без package-подкаталога в соответствии с Thunderstore override rules, поэтому конфликты конфигураций обнаруживаются явно. Файлы `*.mm.dll` направляются в `BepInEx/monomod/<owner>/`. Внутренняя структура каталогов сохраняется.

Обычные runtime-файлы устанавливаются в `BepInEx/plugins/<owner>/`; промежуточные каталоги вне override-папок игнорируются согласно текущему поведению r2modman. Служебные корневые файлы `manifest.json`, `README.md`, `CHANGELOG.md` и `icon.png` в игру не копируются.

Правила основаны на [официальном формате package Thunderstore](https://wiki.thunderstore.io/mods/creating-a-package), [официальной установке BepInEx](https://docs.bepinex.dev/master/articles/user_guide/installation/unity_mono.html) и актуальном [описании BepInEx packaging в r2modman](https://github.com/ebkr/r2modmanPlus/wiki/Structuring-your-Thunderstore-package). r2modman используется только как reference implementation; управление файлами, state и транзакциями остаётся архитектурой ModSync.

## Dry Run

```bash
modsync install modpack.json --dry-run
```

Команда разрешает sources и dependencies, скачивает и проверяет packages, строит и полностью валидирует InstallationPlan, затем показывает destination-пути. Она не изменяет game root, не создаёт production state и не создаёт backup. Тот же флаг доступен для `update`.

## File Ownership

Adapter-state хранит каждый установленный destination вместе с owner и SHA256. Verifier использует эти записи напрямую и не пытается повторно угадывать routing по ZIP. Эта модель подготовлена для будущих uninstall, disable, profile switching и orphan cleanup, но сами эти операции в v0.6.0 не реализованы.

## Installation Conflicts

Весь план проверяется до записи первого игрового файла. Установка останавливается, если два package претендуют на один destination, если collision возникает только из-за регистра на case-insensitive платформе, либо если destination уже занят unmanaged-файлом. Замена разрешена только тому же owner; при update предыдущий файл попадает в backup.

## Uninstall

```bash
modsync uninstall ExampleMod --profile friends-server
modsync uninstall ExampleMod modpack.json --dry-run
```

Uninstall строит полный `RemovalPlan`, проверяет ownership и dependents, создаёт backup и удаляет только managed-файлы выбранного package. Shared-файлы с неоднозначным владельцем, unsafe state paths, symlink/hardlink и изменённые runtime-файлы блокируют операцию. Для намеренного удаления изменённого бинарного файла доступен `--force`, но он не отключает проверки путей, ownership или ссылок.

## Enable / Disable

```bash
modsync disable ExampleMod
modsync enable ExampleMod
```

`disable` временно убирает runtime-файлы мода из BepInEx, сохраняя package и state в ModSync. `uninstall` удаляет управляемые файлы мода из игры и его запись из state.

Runtime-файлы отключённого package хранятся вне каталогов загрузчика:

- profile-mode: `profiles/<profile>/disabled/<package>/...`;
- adapter-mode без profile: `<game-root>/.modsync-disabled/<package>/...`.

Исходная относительная структура, owner и SHA256 сохраняются. Enable проверяет checksum disabled content, наличие включённых dependencies и отсутствие unmanaged/case-insensitive destination conflicts до восстановления файлов. Нельзя отключить dependency, пока от неё зависит включённый mod.

## Managed Files

State явно хранит `status` (`enabled` или `disabled`), `install_reason` (`explicit` или `dependency`), package owner, dependency names и расположение каждого managed-файла. Для state v0.5/v0.6 безопасные defaults — `enabled` и `explicit`; Thunderstore dependency metadata v0.6 также используется при dependency safety checks.

## Configuration Preservation

Config-файлы в `BepInEx/config` не перемещаются при disable. При uninstall неизменённый managed config может быть удалён, а изменённый пользователем config сохраняется на месте и становится unmanaged. ModSync всегда сообщает `Preserved modified configuration` и не уничтожает пользовательские настройки даже с `--force`.

## Orphan Dependencies

После удаления explicit-мода ModSync анализирует оставшиеся dependency records. Больше не используемые dependencies не удаляются автоматически, а выводятся как потенциальные orphan packages. Автоматический `cleanup` в v0.7.0 намеренно не реализован.

Для всех трёх операций доступен `--dry-run`: он строит и проверяет план, но не меняет game root, disabled storage, state или backups. Реальные операции используют общий BackupManager schema v3 для game и disabled roots; при ошибке тот же rollback engine восстанавливает файлы и state.

## Профили

Профиль хранит копию modpack, state и backup независимо от других профилей. Активный профиль — это профиль по умолчанию для команд без `modpack.json` и `--profile`. Явный `--profile` имеет приоритет над активным.

Служебные данные хранятся в стандартном каталоге пользователя:

- macOS: `~/Library/Application Support/ModSync/`;
- Windows: `%LOCALAPPDATA%/ModSync/`;
- Linux: `$XDG_DATA_HOME/modsync/` или `~/.local/share/modsync/`.

```text
ModSync/
├── config.json                 # активный профиль
├── locks/                     # межпроцессные блокировки
└── profiles/
    └── friends-server/
        ├── profile.json       # metadata профиля
        ├── modpack.json       # сохранённая копия modpack
        ├── state.json         # state только этого профиля
        └── backups/           # backup только этого профиля
```

Изменяющие операции `install`, `update` и `backup restore` защищены блокировкой на профиль. Параллельные команды чтения не блокируются. Команда `profile delete` требует подтверждения; `--yes` его пропускает. Удаляются только служебные данные ModSync; папка игры и установленные моды не удаляются.

## Backup & Rollback

Перед командой `update` ModSync сначала скачивает и полностью проверяет все необходимые обновления во временной директории. Текущая установка не изменяется, пока загрузки, SHA256 и ZIP-архивы не пройдут проверку.

Если обновление действительно меняет моды, ModSync создаёт backup в каталоге:

```text
<install_directory>/.modsync-backups/<backup-id>/
├── metadata.json
├── state.json
└── files/
```

Backup содержит только относительные пути, контрольные суммы сохранённых файлов, версии затрагиваемых модов и снимок локального состояния. В профильном режиме backup хранятся в `profiles/<name>/backups/`, а не в папке игры. По умолчанию после успешного обновления сохраняются пять последних backup. Очистка старых копий не выполняется до успешного создания новой копии и завершения update.

Посмотреть доступные backup:

```bash
modsync backup list modpack.json
```

Восстановить выбранный backup:

```bash
modsync backup restore modpack.json 20260916T153012Z-a4f21c
```

Перед восстановлением ModSync проверяет metadata, безопасность всех путей, SHA256, state-файл, символические и жёсткие ссылки. Изменение текущей установки начинается только после полной проверки. Восстанавливаются моды и записи state, относящиеся к выбранному backup; более поздние изменения других модов сохраняются. Если update завершается ошибкой после начала применения файлов, ModSync автоматически пытается восстановить созданный backup и отдельно сообщает результат rollback, не скрывая первоначальную ошибку.

## Формат модпака

Путь `install_directory` определяется относительно JSON-файла. Названия модов должны быть уникальными. Поле SHA256 необязательно, однако его настоятельно рекомендуется заполнять, если издатель предоставляет доверенную контрольную сумму.

```json
{
  "name": "Karim Valheim Pack",
  "version": "1.0.0",
  "description": "Модпак для игры с друзьями",
  "game": "Valheim",
  "install_directory": "./mods",
  "mods": [
    {
      "name": "ExampleMod",
      "version": "1.2.0",
      "url": "https://example.com/ExampleMod.zip",
      "sha256": null,
      "enabled": true
    }
  ]
}
```

Готовые для редактирования примеры находятся в файлах [`examples/modpack.example.json`](examples/modpack.example.json), [`examples/modpack.github.example.json`](examples/modpack.github.example.json), [`examples/modpack.thunderstore.example.json`](examples/modpack.thunderstore.example.json) и [`examples/modpack.valheim.example.json`](examples/modpack.valheim.example.json).

## Источники модов

Каждый мод может описывать источник в объекте `source`. Старый формат с полями `version` и `url` остаётся полностью совместимым и не требует миграции.

### Direct URL

Прямая HTTP(S)-ссылка подходит для файлов с фиксированным адресом:

```json
{
  "name": "Example",
  "source": {
    "type": "direct",
    "url": "https://example.com/mod.zip"
  }
}
```

Для direct-источника рекомендуется указывать `version` на уровне мода или внутри `source`: без неё ModSync использует значение `unversioned` и не может узнать об изменении удалённого файла по одному и тому же URL.

### GitHub Releases

GitHub-источник получает опубликованный стабильный релиз через официальный API, выбирает ровно один asset и сохраняет tag, release/asset ID, URL, время разрешения и фактический SHA256 в state:

```json
{
  "name": "Example",
  "source": {
    "type": "github",
    "repository": "owner/project",
    "release": "latest",
    "asset": "Example-*.zip"
  }
}
```

В `release` можно указать `latest` или конкретный tag, например `v1.4.2`. Поле `asset` поддерживает точное имя и glob-шаблон. Совпасть должен ровно один прикреплённый к релизу файл: автоматически созданные GitHub source archives не выбираются.

Публичные репозитории работают без авторизации. При ограничении частоты запросов можно передать personal access token только через переменную окружения:

```bash
export MODSYNC_GITHUB_TOKEN="..."
modsync update modpack.json
```

В PowerShell:

```powershell
$env:MODSYNC_GITHUB_TOKEN = "..."
modsync update modpack.json
```

Не добавляйте токен в `modpack.json`. ModSync передаёт его только в HTTP-заголовке и не сохраняет в state, логах или backup.

Типичные ошибки:

- `rate limit` — подождите сброса лимита или задайте `MODSYNC_GITHUB_TOKEN`;
- `release not found` — проверьте `owner/repository` и tag в поле `release`;
- `asset not found` — проверьте имя файла или glob-шаблон;
- `ambiguous assets` — уточните шаблон так, чтобы он совпадал ровно с одним asset.

### Thunderstore

Thunderstore-источник получает metadata пакета через публичный read-only API, проверяет выбранное community и скачивает официальный ZIP-архив. Ключ API не требуется.

```json
{
  "name": "BepInExPack Valheim",
  "source": {
    "type": "thunderstore",
    "community": "valheim",
    "namespace": "denikson",
    "package": "BepInExPack_Valheim",
    "version": "latest"
  }
}
```

Поле `version` принимает:

- `latest` — при каждой команде `install` или `update` разрешается актуальная версия;
- точную версию вида `1.2.3` — ModSync не заменяет её более новой.

Перед любым изменением установки ModSync рекурсивно строит весь граф зависимостей. Каждая зависимость Thunderstore содержит точную версию. Одинаковые пакеты скачиваются один раз, зависимости устанавливаются раньше зависящего пакета, а конфликты версий и циклы останавливают операцию до скачивания. Сначала скачиваются и проверяются все ZIP-файлы и `manifest.json`, и только затем начинается установка.

Для `latest` пакета и его зависимостей обновляются вместе. Если зависимость больше не нужна, она остаётся как orphan: ModSync не удаляе её автоматически. Для устаревшего (`deprecated`) пакета с `latest` выводится предупреждение, но замена не выбирается автоматически.

Команда `modsync info` показывает источник, установленную версию и статус `latest`, `pinned` или `dependency`.

Типичные ошибки:

- `package ... was not found` — проверьте `namespace` и `package`;
- `community ... was not found` — пакет не опубликован для указанного community;
- `Dependency conflict` — два пакета требуют разные точные версии одной зависимости;
- `Circular dependency` — в metadata найден цикл;
- `manifest ... mismatch` — скачанный `manifest.json` не совпадает с metadata API;
- `rate limit` или `Could not query Thunderstore` — сервис временно недоступен; повторите команду позже.

## Структура проекта

```text
modsync/
├── modsync/       # CLI, adapters, профили, sources, backup, installer и verifier
│   └── games/     # GameAdapter, GameRegistry и ValheimAdapter
├── tests/         # Модульные тесты без реальных сетевых запросов
├── examples/      # Пример модпака
├── pyproject.toml # Метаданные пакета и точка входа CLI
├── README.md
└── LICENSE
```

## Безопасность

ModSync считает каждую загрузку и каждый backup недоверенными данными и никогда не запускает сохранённые файлы. Все элементы ZIP проверяются перед распаковкой: абсолютные пути, переходы в родительские директории, пути с указанием диска и символические ссылки отклоняются. При восстановлении дополнительно проверяются metadata, относительные пути, SHA256, символические и жёсткие ссылки. Ограничения на количество элементов и суммарный размер распакованных данных снижают риски, связанные с ZIP-бомбами. Установка сначала выполняется во временную директорию внутри настроенного каталога, а проверка TLS в `requests` всегда остаётся включённой.

Для максимальной защиты используйте HTTPS-ссылки и указывайте `sha256`, полученный из доверенного источника. Контрольная сумма подтверждает идентичность файла, но не безопасность самого мода. Проверяйте моды и их издателей перед загрузкой в игру.

## Разработка

```bash
python -m pip install ".[dev]"
python -m pytest
```

Тесты проверяют конфигурации, direct/GitHub/Thunderstore sources, графы зависимостей, профили, изоляцию state и backup, locking, обратную совместимость CLI, вычисление хешей, безопасную установку ZIP, backup/restore, rollback, retention и сетевые ошибки. Реальные сетевые запросы в тестах не выполняются.

## Планы развития

- [x] Direct URL.
- [x] GitHub Releases.
- [x] Thunderstore и рекурсивные зависимости.
- [x] Game Adapter architecture.
- [x] Valheim + BepInEx.
- [x] Uninstall.
- [x] Enable / Disable.
- [ ] Настоящее переключение файлов между profiles.
- [ ] Автоматическое обнаружение игры.
- [ ] GUI.

Это планы на будущее, а не возможности текущей MVP-версии.

## Текущие ограничения

- Поддерживаются direct URL, GitHub Releases и Thunderstore; Nexus Mods и локальные/custom providers пока не реализованы.
- Для проверки установленных версий необходимо сохранить локальный файл состояния.
- Отключённые или удалённые из модпака моды не удаляются автоматически.
- Backup создаётся автоматически для `update`; отдельной команды ручного создания пока нет.
- Retention фиксирован на пяти последних backup и пока не настраивается.
- ModSync не переключает автоматически настройки самой игры; active profile выбирает контекст команд ModSync.
- Adapter-mode в v0.7.0 поддерживает только Valheim + BepInEx; автоматическая установка BepInEx выполняется только если loader является явным package/dependency.
- Lifecycle-команды требуют ownership state adapter-mode; legacy installation pipeline продолжает работать, но небезопасное угадывание ownership для старых directory-only записей не выполняется.
- Orphan dependencies только анализируются и не удаляются автоматически; команда `cleanup` зарезервирована для будущей версии.
- Если несколько профилей указывают одинаковый `install_directory`, ModSync показывает предупреждение. Profiles share the same physical mod directory. Their ModSync state and backups remain separate, but installed files may overlap.
- Откат использует безопасное best-effort поведение и не заявляет абсолютную транзакционность файловой системы на всех платформах.
- Автоматическое разрешение зависимостей доступно только для Thunderstore; автоудаление orphan-зависимостей и команда предпросмотра графа пока не реализованы.
- Публичный Thunderstore API не требует аутентификации; ModSync не реализует загрузку/публикацию пакетов и GUI.
- ModSync не определяет, является ли скачанный мод безопасным и совместимым с игрой.

## Лицензия

ModSync распространяется на условиях [лицензии MIT](LICENSE).
