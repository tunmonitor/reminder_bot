# reminder_bot

Telegram-бот напоминаний по расписанию для Raspberry Pi.

## Установка

```bash
cd /home/pi/app/reminder_bot/prod      # или .../test
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp reminder_bot_credentials.env.example reminder_bot_credentials.env
# ... заполнить BOT_TOKEN, ADMIN_TG_ID, GROUP_CHAT_ID, YANDEX_PUBLIC_URL

Управление
./reminder_bot_manage.sh start
./reminder_bot_manage.sh status
./reminder_bot_manage.sh logs
./reminder_bot_manage.sh debug-logs
./reminder_bot_manage.sh logs --follow
./reminder_bot_manage.sh backup
./reminder_bot_manage.sh stop

Команды `logs` и `debug-logs` по умолчанию выводят последние строки и завершаются. Для непрерывного слежения укажите `--follow`.

Аргументы CLI бота
Бот запускается только через manage.sh и всегда принимает:

--shm-dir — директория ОЗУ (/dev/shm/reminder_bot/{test|prod})

--log-dir — директория логов (по умолчанию = --shm-dir)

При запуске через `manage.sh` директорию логов можно переопределить переменной `LOG_DIR`, не меняя `SHM_DIR`, например: `LOG_DIR=/var/log/reminder_bot ./manage.sh start`.

Каталог `--shm-dir` должен принадлежать пользователю бота и не быть доступен на запись группе или другим пользователям. `manage.sh` настраивает каталоги ОЗУ с правами `700`; PID-файл создается эксклюзивно с правами `600`.

Файлы логов и бэкапы создаются с правами `600`; каталог бэкапов `logs_backup` настраивается с правами `700`. Debug-лог содержит найденные для рассылки имя, Telegram username, Telegram ID и подготовленные тексты сообщений; не публикуйте этот файл.

Если по части ролей не хватает назначения или Telegram ID, бот уведомляет администратора, но всё равно отправляет сообщение в группу и личные сообщения участникам, чьи данные сопоставлены. Для неизвестной групповой отметки используется текст «не найден».

Проверка Excel без рассылки
Для отладки скачивания и парсинга используйте отдельную команду, которая не запускает бота и не отправляет сообщения:

```bash
/home/pi/.venv/bin/python -m bot.debug_schedule
/home/pi/.venv/bin/python -m bot.debug_schedule --date 2026-10-11
```

Файл и debug-лог сохраняются вместе и не удаляются автоматически в
`/dev/shm/reminder_bot/debug/` (права каталога `700`, файлов `600`).
Путь к скачанному файлу печатается в выводе команды.
Можно указать другой приватный каталог через `--output-dir`; каталог должен
принадлежать текущему пользователю и быть недоступен группе и другим.
Удаляйте сохраненные файлы вручную после отладки.

Проектный skill для повторного использования этого workflow находится в
`.github/skills/reminder-bot-xlsx-debug/SKILL.md`.

Структура
reminder_bot.py — точка входа

bot/cli.py — argparse

bot/debug_schedule.py — безопасная отладочная проверка xlsx без рассылки

bot/config.py — загрузка креденшелов/шаблонов/расписания

bot/logger.py — interaction + debug логгеры

bot/state.py — кэш текущей недели

bot/yandex_disk.py — скачивание xlsx с retry 3×5 мин

bot/excel_parser.py — парсинг месячных листов расписания и «Списки»
Парсер пересчитывает фактические размеры листов, если в xlsx ошибочно указан диапазон A1, и нормализует известный некорректный стиль границ только в памяти, не меняя исходный файл.

bot/scheduler.py — APScheduler

bot/notifier.py — веерная рассылка с rate limit 0.05 c

bot/handlers.py — callback «Принял, буду»
Регистрация обработчиков выполняется через `register(dispatcher, context)`, чтобы роутер обязательно подключался к диспетчеру.

bot/app.py — сборка, сигналы, PID-файл
