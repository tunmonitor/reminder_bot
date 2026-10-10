---
name: reminder-bot-xlsx-debug
description: "Use when testing or debugging the reminder bot's Yandex Disk Excel schedule, parsing monthly sheets, mapping Telegram contacts, or simulating notifications without sending Telegram messages."
user-invocable: true
---

# Reminder bot Excel diagnostics

Use this workflow to inspect the live schedule safely, diagnose workbook/parser
problems, and preview messages without contacting Telegram users.

## Safety requirements

- Run Python commands with `/home/pi/.venv/bin/python`.
- Never print or expose `BOT_TOKEN`, the full credentials file, or unrelated
  participant records.
- Do not call the Telegram send methods during a test or dry-run.
- Use `python -m bot.debug_schedule`; it downloads a private copy, parses the
  schedule, and renders messages without sending them.
- Keep downloaded workbooks and logs in `/dev/shm/reminder_bot/debug/`.
  The directory should be mode `700`, and files mode `600`.
- Debug logs contain names, usernames, Telegram IDs, and prepared message text.
  Treat them as private.

## Procedure

1. Confirm the project root and load credentials through the application;
   do not display credential values.
2. Run the dry-run for a specific Sunday:

   ```bash
   /home/pi/.venv/bin/python -m bot.debug_schedule --date YYYY-MM-DD
   ```

   Omit `--date` to use the next Sunday in the configured timezone.
3. Record the printed xlsx path. Artifacts are retained in
   `/dev/shm/reminder_bot/debug/` and are not automatically deleted.
4. Inspect `reminder_bot_debug.log` for HTTP status, detected monthly sheet,
   parsed task assignments, resolved contacts or missing fields, and the final
   group/private message previews.
5. For a monthly schedule, verify that the sheet name is the Russian month plus
   two-digit year (for example, `ОКТЯБРЬ26`). Dates are separate rows in column
   A; task labels and assignees are in A and B.
6. The task aliases are case-insensitive:
   - `ХКЦ` → `Ответственный`
   - `Ведущий` or `Ведет` → `Ведущий`
   - `Проповедует` or `Проповедет` → `Проповедник`
7. On `Списки`, task headers can occur on any row in column A. Participant
   name, username, and Telegram ID are in columns A, B, and C. Partial
   records are tolerated; only an assigned participant with a valid positive
   Telegram ID receives a personal message.
8. Missing individual assignments or Telegram IDs should generate an admin
   warning but must not block the group message or personal messages to other
   valid recipients. Workbook download/read failures are fatal to that run.
9. If parsing fails, preserve the downloaded workbook and diagnose its XML
   structure without dumping cell contents. Known workbook quirks include an
   incorrect worksheet dimension such as `A1` and border style `solid`, which
   the parser handles without modifying the original file.
10. After source-code changes, run the focused parser/notifier tests and then:

    ```bash
    /home/pi/.venv/bin/python -m unittest discover -s tests
    ```

## Relevant files

- `bot/debug_schedule.py` — persistent dry-run command.
- `bot/excel_parser.py` — monthly worksheet and contacts parsing.
- `bot/notifier.py` — shared message formatting and actual notification send.
- `bot/app.py` — scheduled workflow and admin warnings.
- `bot/logger.py` — private interaction/debug log setup.
