---
name: reminder-bot-project
description: "Use when developing, reviewing, testing, or debugging any part of reminder_bot: Telegram handlers and service menus, group or personal reminders, Excel/Yandex Disk schedules, SQLite registry, backups, deployment, or the static Web App."
user-invocable: true
---

# reminder_bot project workflow

Use this skill for project-wide work in `tunmonitor/reminder_bot`, including
code changes, reviews, tests, operational diagnosis, and documentation.
Project requirements and examples are also recorded in `README.md`, `tz2.txt`,
and `UI_SKETCHES.md`; check the relevant section when behavior is ambiguous.

## Safety and working-tree rules

- Run project Python commands with `/home/pi/.venv/bin/python`.
- Start by checking `git status --short`. Preserve existing user changes; never
  revert unrelated edits or commit/push unless asked.
- Never print `BOT_TOKEN`, credential files, full participant records, or
  private message contents. Debug logs and schedule workbooks may contain names,
  usernames, Telegram IDs, assignments, and rendered message text; treat them
  as private.
- Do not send Telegram messages during tests, dry-runs, or previews. Do not
  invoke live delivery paths to validate rendering.
- Do not delete original workbooks or broad directories. Keep diagnostic
  workbooks/logs under `/dev/shm/reminder_bot/debug/` with directory mode 700
  and file mode 600; these artifacts are not automatically removed.
- Do not stop, restart, deploy, restore, or mutate a live environment unless
  explicitly requested. For restore operations, the bot must be stopped and
  the snapshot environment must match the target.
- Prefer focused unit tests first, then the complete suite when a code change
  is ready. Report warnings separately from test failures.

## Project map

- `reminder_bot.py`, `bot/reminder_bot.py`, `bot/app.py` — application entry
  points, lifecycle, dependency wiring, scheduling, and notification flows.
- `bot/handlers.py` — Telegram commands, callback handlers, access checks,
  conversation state, service setup, and personal notification screens.
- `bot/service_registry.py` — SQLite schema, migrations, settings, contacts,
  reminder schedules, delivery records, and configuration audit.
- `bot/excel_parser.py`, `bot/yandex_disk.py` — workbook download, schedule
  parsing, aliases, contacts, and assignment resolution.
- `bot/presentation.py`, `bot/notifier.py` — shared assignment/message
  formatting, sending, delivery handling, and administrator warnings.
- `bot/scheduler.py` — cron jobs, reminder occurrence/date rules, and refresh.
- `bot/state.py` — persisted delivery/confirmation state cache.
- `bot/ui.py` — shared Telegram screen rendering, button layout, pagination,
  callback limits, and menu cleanup.
- `bot/backup.py`, `bot/backup_cli.py`, `bot/secure_fs.py` — private,
  validated snapshots, restore, filesystem safety, and instance locking.
- `bot/webapp_server.py`, `webapp/` — static Web App only; there is no data API.
- `bot/debug_schedule.py` — private Excel schedule dry-run without delivery.
- `tests/` — `unittest` suite; `.github/workflows/deploy.yml` runs it in CI
  before eligible deployments.

## Development procedure

1. Read the relevant implementation and tests, then check `README.md`,
   `tz2.txt`, or `UI_SKETCHES.md` for project-specific behavior.
2. Trace the full affected path (entry point, callback/message handler,
   registry, scheduler/notifier, and UI as applicable). Reuse shared helpers
   instead of adding parallel logic.
3. Make a surgical change that preserves permissions, callback formats,
   confirmation steps, delivery idempotency, and user-visible behavior unless
   the task explicitly changes them. Surface failures through existing logs,
   administrator notifications, or recoverable UI; do not silently fall back.
4. Add or update focused tests for both successful behavior and relevant edge
   cases. Use mocks/fakes for Telegram, Yandex Disk, clocks, and database
   failures; do not send messages to real users.
5. Run focused tests, then (for code changes) the full suite and syntax/diff
   checks:

   ```bash
   /home/pi/.venv/bin/python -m unittest tests.test_<area> -v
   /home/pi/.venv/bin/python -m unittest discover -s tests -q
   /home/pi/.venv/bin/python -m compileall -q bot tests
   git diff --check
   ```

   Choose existing test modules relevant to the change; do not literally run
   the `<area>` placeholder. Documentation-only changes need no tests unless
   documentation tests exist.

## Domain rules and invariants

### Excel schedule and contacts

- Monthly sheets are named with a Russian month and two-digit year, e.g.
  `ОКТЯБРЬ26`. Schedule dates are in column A; task names and assignees are in
  columns A and B. The `Списки` sheet contains task headers in A and contact
  name, username, and Telegram ID in A, B, and C.
- Task aliases are case-insensitive: `ХКЦ` → `Ответственный`; `Ведущий` or
  `Ведет` → `Ведущий`; `Проповедует` or `Проповедет` → `Проповедник`.
- Informational tasks may be marked with a leading `*`, `[...]`, or `[*]`.
  They remain available to group templates as `{informational_notes}`, but
  are excluded from responsible-contact selection, personal reminders, and
  `{task_assignments}`. A group reminder with only informational tasks is not
  sent.
- A missing individual assignment/contact/valid positive Telegram ID should
  be reported without preventing valid recipients or the group from receiving
  their messages. Workbook download/read failures are fatal for that run.
- For safe diagnosis, use the dry-run rather than sender paths:

  ```bash
  /home/pi/.venv/bin/python -m bot.debug_schedule --date YYYY-MM-DD
  ```

  Omit `--date` to use the next target Sunday. Preserve downloaded files when
  diagnosing parser failures; inspect workbook structure without dumping
  participant cell values. Known quirks include incorrect worksheet dimension
  `A1` and invalid border style `solid`; parser recovery must not modify the
  original workbook.

### Group and personal reminders

- Group reminders are independent records with their own `reminder_days`,
  `reminder_time`, enabled state, and optional body. `body = NULL` resolves
  through the service default template and then the built-in default.
- Templates must render through the same resolver/value mapping as real
  delivery. Unknown legacy fields remain literal when the production renderer
  supports that behavior. Supported group fields include `{service_name}`,
  `{sunday_date}`, `{task_assignments}`, and `{informational_notes}`.
- `next_reminder_occurrence(now, reminder_days, reminder_time)` selects the
  earliest Sunday whose send date/time has not passed. Send date is event
  Sunday minus `reminder_days`; a passed send time selects the next eligible
  Sunday. This includes a zero-day Sunday reminder before its configured time.
  Use the shared helper for cards and previews; do not reimplement date math.
- Preview must not deliver messages. It displays event date separately from
  send date/time, rejects informational-only schedules, and catches workbook
  parsing failures with a logged, navigable error screen.
- Callback-driven asynchronous work must validate the active session, screen,
  item ID, and request-specific token before loading and after every relevant
  `await`. New previews invalidate old requests; old results must not replace
  a newer menu.
- Preserve idempotent delivery records and per-service/event locking. A
  failed delivery must not be recorded as success.

### Service-selection menus and Telegram UI

- All service-selection commands (`/services`, `/service_settings`,
  `/service_contact`, `/service_notify`, `/service_chat`) use the shared
  service ordering and button factory where appropriate.
- Up to eight service buttons are shown without pagination in the standard
  compact layout (normally 3+3+2). Larger lists paginate within Telegram's
  button limit. Page navigation belongs in a separate bottom row; include
  navigation and pinned actions when calculating page capacity.
- `layout_buttons` should balance rows while respecting label width and the
  three-button-per-row limit. Try alternate contiguous boundaries at the same
  row count before adding a row. Keep long/single-action buttons on their own
  row and navigation at the bottom.
- Validate callback data (maximum 64 UTF-8 bytes), keyboard row/count limits,
  and Telegram text limits (UTF-16 code units). Preserve callback data and
  ordering when changing layouts.
- Conversation handlers must reject stale sessions and enforce service/admin
  permissions before mutating state. Keep callback toasts short and plain.

### SQLite, backups, and restore

- Use `ServiceRegistry` methods for persistent state; avoid ad hoc SQL except
  in migrations or focused tests. Migrations must be safe to rerun and
  preserve legacy data/IDs where compatibility requires it.
- Snapshots are environment-specific and must be fully written, validated
  (manifest/checksums and SQLite integrity), then atomically published.
  Credentials, tokens, logs, and temporary files do not belong in a snapshot.
- Current retention is bounded to 14 validated snapshots per environment and
  retains at least 7 recovery snapshots. Prune only older validated snapshots
  after a new snapshot has been committed; do not delete invalid/unvalidated
  recovery data as if it were safely disposable.
- Restore validates all components and environment before replacing targets,
  uses staging beside targets, and refuses unsafe/running-bot conditions.
  Never auto-delete SQLite `-wal`/`-shm` files to force a restore.

### Web App and deployment

- `webapp/` is static content served from its own root. Do not expose project,
  configuration, database, or runtime files. Keep it bound behind the
  configured HTTPS reverse proxy; the static server is not an authenticated
  application API.
- Never put tokens or private data in browser assets, URLs, diagnostics, or
  logs. Do not treat client-side `initDataUnsafe` as authentication.
- `manage.sh` controls bot, Web App, logs, backups, and restore in test/prod
  deployments. Test and prod use separate directories/configuration. Avoid
  deployment commands during ordinary code changes.

## Focused test modules

- Excel parser and informational-task behavior: `tests/test_excel_parser.py`
- Schedule and reminder occurrence rules: `tests/test_scheduler.py`,
  `tests/test_service_preview.py`
- Telegram workflows, sessions, permissions, and service menus:
  `tests/test_handlers.py`
- Button layout, pagination, screen rendering: `tests/test_ui.py`
- Registry and migrations: `tests/test_service_registry.py`,
  `tests/test_config_service_registry.py`
- Snapshot creation, retention, and restore: `tests/test_backup.py`
- Web App static server/config: `tests/test_webapp_server.py`,
  `tests/test_config_service_registry.py`
- Runtime/CLI setup and dry-run: `tests/test_runtime_entrypoint.py`,
  `tests/test_debug_schedule.py`

## Relevant project documentation

- `README.md` — setup, commands, operations, and development workflow.
- `tz2.txt` — detailed behavioral, UI, data, backup, and Web App
  requirements.
- `UI_SKETCHES.md` — representative Telegram screen layouts.
