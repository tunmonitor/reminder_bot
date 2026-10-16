```markdown
# reminder_bot

Telegram-бот напоминаний по расписанию для Raspberry Pi.

## Возможности

- Групповые напоминания в чаты служений по настраиваемому расписанию.
- Персональные напоминания участникам с кнопкой подтверждения.
- Отдельные тексты и расписания для каждого пользователя.
- Пометки о событиях дня (причастие, праздник) в графике.
- Управление несколькими служениями, администраторами и привязками.
- Раздача Telegram Web App через отдельный HTTP-сервер.
- SQLite-реестр, аудит изменений, резервное копирование и
  восстановление.

## Установка

```bash
cd /home/pi/app/reminder_bot/prod      # или /mnt/external_data/reminder_bot/test
python3 -m venv venv                   # только если venv ещё не создан
./venv/bin/pip install -r requirements.txt
cp reminder_bot_credentials.env.example reminder_bot_credentials.env
# Заполнить BOT_TOKEN, ADMIN_TG_ID, GROUP_CHAT_ID, YANDEX_PUBLIC_URL.
cp webapp_config.example.json webapp_config.json
# При необходимости задать отдельный порт для этого окружения.
```

Зависимости устанавливаются в отдельное `venv/` каждого развёртывания.
На Raspberry Pi для работы из исходного репозитория используется
`/home/pi/.venv`. Скрипт `manage.sh` определяет `test` или `prod` по
имени каталога; команды запускаются из каталога развёртывания, не из
`src/`.

Совместимость: Python 3.10+, aiogram 3.x, APScheduler 3.x,
openpyxl 3.1+. Raspberry Pi OS (Bookworm, 64-bit).

## Управление

### Бот

```bash
./manage.sh start
./manage.sh status
./manage.sh restart
./manage.sh stop
```

`start` запускает бота. `stop` сохраняет текущий журнал взаимодействий
и останавливает бота. `restart` выполняет остановку и запуск. `status`
печатает `БОТ РАБОТАЕТ` или `БОТ ОСТАНОВЛЕН`.

### Web App

```bash
./manage.sh webapp start
./manage.sh webapp status
./manage.sh webapp restart
./manage.sh webapp stop
```

Web App — статическая страница в `webapp/`, раздаётся отдельным
HTTP-сервером. Параметры — в `webapp_config.json` (host, port,
public_url). Test и prod используют разные порты. Публичный доступ —
только через HTTPS reverse proxy.

### Логи, бэкапы, восстановление

```bash
./manage.sh logs
./manage.sh debug-logs
./manage.sh logs --follow
./manage.sh debug-logs --follow
./manage.sh backup
./manage.sh backup-data
./manage.sh restore-data /mnt/external_data/reminder_bot/backups/prod/<snapshot>
```

`backup` сохраняет interaction-лог. `backup-data` создаёт проверенный
снимок SQLite, SQL-экспорта, кэша, XLSX и JSON. Снимки хранятся в
`/mnt/external_data/reminder_bot/backups/{test|prod}/`. При отсутствии
внешнего диска операция завершается ошибкой.

Перед восстановлением остановите бота:

```bash
./manage.sh stop
./manage.sh restore-data /mnt/external_data/reminder_bot/backups/test/<snapshot>
./manage.sh start
```

`restore-data` проверяет окружение, manifest, контрольные суммы и
целостность SQLite. Восстановление из другого окружения запрещено.
При обнаружении SQLite -wal/-shm сначала разберитесь с их состоянием;
автоматическое удаление не выполняется.

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `SHM_BASE` | Корень оперативных каталогов | `/dev/shm/reminder_bot` |
| `SHM_DIR` | Каталог PID, xlsx, кэша | `$SHM_BASE/<env>` |
| `LOG_DIR` | Каталог логов | значение `SHM_DIR` |
| `REMINDER_BOT_DB_PATH` | Путь SQLite-реестра | `<project>/.runtime/reminder_bot.sqlite3` |
| `TIMEZONE` | Часовой пояс планировщика | `Europe/Moscow` |

Пример:

```bash
SHM_DIR=/dev/shm/reminder_bot/test LOG_DIR=/dev/shm/reminder_bot/test/logs ./manage.sh start
```

## Команды бота

### Для администратора

- `/start` — приветствие.
- `/services` — список доступных служений.
- `/service_settings` — карточка служения (чат, шаблоны, напоминания).
- `/service_admin_add` — назначить администратора служения.
- `/service_admin_remove` — снять администратора.
- `/service_chat` — настроить или отключить групповой чат.
- `/service_contact` — привязать ответственных к задачам из графика.
- `/service_sync` — перечитать график и сверить привязки.
- `/service_notify` — тексты и расписания напоминаний.
- `/test_notify` — проверка доставки.
- `/cancel` — прервать текущий диалог.

### Для пользователя

- `/my_notifications` — личные уведомления: включение по служениям,
  свой текст, свои расписания.
- `/cancel`, `/start`.

Команды работают только в личном чате с ботом. Служение выбирается
кнопкой. Супер-администратор видит все служения, администратор — только
назначенные.

## Сценарии

### Первичная настройка служения (для супер-администратора)

1. `/services` — проверить список.
2. `/service_chat` → выбрать служение → выбрать группу, где бот уже
   состоит.
3. `/service_admin_add` → выбрать служение → выбрать пользователя или
   ввести `123456789 @username` → подтвердить.
4. `/service_sync` → выбрать служение → сопоставить ответственных из
   графика с Telegram-пользователями.
5. `/service_notify` → выбрать служение → настроить напоминания,
   групповой текст и личные шаблоны.
6. `/test_notify` → выбрать служение → проверить отправку.

### Ежедневная поддержка

- После обновления графика — `/service_sync`, чтобы сопоставить новых
  ответственных.
- При смене группы — `/service_chat`.
- Правка текстов — `/service_notify`.

### Пользователь настраивает личные уведомления

1. `/my_notifications`.
2. Сводный экран — выбор служения.
3. Включить уведомления в служении.
4. Открыть список текстов → выбрать задачу → задать свой текст или
   оставить шаблон админа.
5. Открыть расписания → добавить правило (например, «за 2 дня, 09:00»).
6. Закрыть. Напоминания будут приходить по расписаниям.

Личные уведомления выключены по умолчанию для каждого служения.
Пользователь может включить их сам. Групповые уведомления работают
независимо и не зависят от личных настроек.

### Информационные задачи в графике

Задачи-пометки о событиях дня помечаются в Excel префиксом `*` или
обрамлением `[...]`:

```
* Причастие (В.П.)
[Праздник]
```

Такие задачи:

- не предлагаются для привязки ответственных;
- не учитываются в счётчике «Привязано N из M»;
- не порождают личных уведомлений;
- остаются видимыми в `/service_notify` с пометкой «инфо».

## Разработка

1. Разработка в ветке `dev` в `/home/pi/app/reminder_bot/src`.
2. PR `dev → test`: unit-тесты на GitHub-hosted runner, затем деплой
   в `/mnt/external_data/reminder_bot/test` self-hosted runner'ом.
3. Тестовый бот остаётся запущенным для ручной проверки.
4. После одобрения — PR `dev → main`. После merge в `main` workflow
   синхронизирует проект в `/home/pi/app/reminder_bot/prod`.

Подробности — в `.github/workflows/deploy.yml` и разделе 7 `tz.txt`.

### Тесты локально

```bash
cd /home/pi/app/reminder_bot/src
mkdir -p /dev/shm/app/reminder_bot/src/.tmp
TMPDIR=/dev/shm/app/reminder_bot/src/.tmp \
    /home/pi/.venv/bin/python -m unittest discover -s tests -v
```

После завершения удалить только созданные тестовые файлы, не
родительские каталоги.

### Отладка парсинга Excel

```bash
/home/pi/.venv/bin/python -m bot.debug_schedule
/home/pi/.venv/bin/python -m bot.debug_schedule --date 2026-10-11
```

Файл и debug-лог сохраняются в `/dev/shm/reminder_bot/debug/`. Сообщения
не отправляются. Удаляйте сохранённые файлы вручную после отладки.

### Проверка целостности SQLite

```bash
python - <<'PY'
import sqlite3
from pathlib import Path
path = Path('.runtime/reminder_bot.sqlite3')
if not path.exists():
    raise SystemExit('SQLite-файл не найден: ' + str(path))
with sqlite3.connect(path) as conn:
    print(conn.execute('PRAGMA integrity_check;').fetchone()[0])
PY
```

Ожидаемый результат: `ok`.

## Структура проекта

```
reminder_bot.py                     — точка входа (argparse --shm-dir/--log-dir)
manage.sh                           — управляющий скрипт
reminder_bot_manage.sh              — симлинк на manage.sh
reminder_bot_credentials.env        — токены (исключён из Git)
reminder_bot_templates.json         — шаблоны (исключён из Git)
reminder_bot_schedule_config.json   — legacy-расписание
webapp_config.json                  — настройки Web App (исключён из Git)
webapp/index.html                   — входная страница Web App
bot/
  cli.py, config.py, logger.py, state.py
  yandex_disk.py, excel_parser.py
  scheduler.py, notifier.py, handlers.py
  service_registry.py, services.py
  presentation.py, ui.py
  backup.py, secure_fs.py
  debug_schedule.py
tests/
requirements.txt
```

Оперативные данные:

```
/dev/shm/reminder_bot/{test|prod}/
  reminder_bot_bot.pid
  reminder_bot_webapp.pid
  reminder_bot_current.xlsx
  reminder_state_cache.json
  reminder_bot_interaction.log
  reminder_bot_debug.log
  reminder_bot_webapp.log
  sys_nohup.log
```

## Безопасность

- `--shm-dir` и `--log-dir` принадлежат пользователю бота, не содержат
  символических ссылок в компонентах пути, недоступны группе и другим.
- Каталоги 700, файлы логов и бэкапов 600, PID-файл создаётся атомарно
  и удаляется владельцем.
- Debug-лог содержит Telegram ID и тексты сообщений — не публиковать.
- Runtime-логи не выгружаются в GitHub artifacts.
- Секреты хранятся только в `credentials.env`, `webapp_config.json`
  и папках развёртывания, исключены из Git.

## Ограничения текущей версии

- Для служений, кроме «Собрания», расписание и настройки сохраняются,
  но плановая отправка подключена частично.
- Web App раздаёт статику; API для чтения/изменения данных бота
  пока не предоставляется.
- Восстановление данных выполняется только при остановленном боте.

## Лицензия и контакты

Внутренний проект. Обратная связь — через issue в репозитории.
```