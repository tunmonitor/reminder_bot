# Reminder bot project notes

## Current status
- Project root: /home/pi/app/reminder_bot/src
- Active runtime environment: /home/pi/.venv
- Verified: unit tests pass with `python -m unittest discover -s tests -v`
- Critical fix implemented: router registration in `bot.handlers.register()`
- Critical fix implemented: `manage.sh` now passes required CLI arguments `--shm-dir` and `--log-dir`
- Critical fix implemented: correct next-Sunday logic in `bot.app._next_sunday_in_tz`
- Critical fix implemented: PID and xlsx filenames now match TZ contract (`reminder_bot_bot.pid`, `reminder_bot_current.xlsx`)

## Important project conventions
- Real runtime folders are expected to be `test` and `prod` under /home/pi/app/reminder_bot/, not `src`.
- Configs are read from the project root directory: `reminder_bot_credentials.env`, `reminder_bot_templates.json`, `reminder_bot_schedule_config.json`.
- Runtime state/logs are stored in /dev/shm/reminder_bot/<env>/.
- The current managed launcher is `manage.sh`, with `reminder_bot_manage.sh` as a symlink alias for compatibility.

## Re-check before further work
- Validate the app in the actual `test` or `prod` folder after copying project files and real config values.
- Confirm Telegram credentials and Yandex public URL are populated before live bot testing.
- Re-run unit tests after any significant bot logic or scheduling change.
