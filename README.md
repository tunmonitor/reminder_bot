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
./reminder_bot_manage.sh backup
./reminder_bot_manage.sh stop

Аргументы CLI бота
Бот запускается только через manage.sh и всегда принимает:

--shm-dir — директория ОЗУ (/dev/shm/reminder_bot/{test|prod})

--log-dir — директория логов (по умолчанию = --shm-dir)

Структура
reminder_bot.py — точка входа

bot/cli.py — argparse

bot/config.py — загрузка креденшелов/шаблонов/расписания

bot/logger.py — interaction + debug логгеры

bot/state.py — кэш текущей недели

bot/yandex_disk.py — скачивание xlsx с retry 3×5 мин

bot/excel_parser.py — парсинг листов «График» и «Список»

bot/scheduler.py — APScheduler

bot/notifier.py — веерная рассылка с rate limit 0.05 c

bot/handlers.py — callback «Принял, буду»

bot/app.py — сборка, сигналы, PID-файл


