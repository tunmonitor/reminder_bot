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
- Put temporary files (test `TMPDIR`, scratch worktrees) under
  `/dev/shm/app/reminder_bot/src/.tmp`, not `/tmp` or the SD card.
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
- `bot/commands.py` — role resolution and per-role Telegram command menus.
- `bot/scheduler.py` — cron jobs, reminder occurrence/date rules, and refresh.
- `bot/state.py` — persisted delivery/confirmation state cache.
- `bot/ui.py` — shared Telegram screen rendering, rich-text markers
  (`code()`, `user_link()`, `plain_text()`, `visible_units()`), button
  layout, pagination, callback limits, and menu cleanup.
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
  name, username, and Telegram ID in A, B, and C (legacy assembly parser
  `parse_people`).
- `parse_service_rosters` reads `Списки` for all services: row 1 holds service
  headers (block runs right to the next header), task cells start a task in
  their column, people below; username/ID sit in the next two columns.
  `ServiceRegistry.sync_service_rosters` stores them in `service_members`
  (file fills only empty Telegram ID/username; writes only on real changes,
  because any write triggers a backup and job refresh). `_load_service_task_
  assignments(..., registry=...)` syncs rosters and `ensure_member_contacts`
  on every schedule read; roster failures are logged and never block
  reminders. Binding UI is person-centric (`bind_service_member` covers all
  the person's tasks); `members-v1` migrates old contacts once.
- Services, tasks, and month-sheet columns come from
  `reminder_bot_services.json` (`bot/services.py` validates it at import).
  `parse_service_events(..., services)` maps column headers to services,
  merges several columns of one service into one event per date, renames
  tasks to catalog names via aliases (`normalized_name`), silently drops
  disabled tasks (and columns whose fixed task is disabled), and reports
  unknown tasks in `ServiceEvent.issues`. Pass
  `registry.service_catalog()` so super-admin task toggles apply.
- `ServiceRegistry.initialize` syncs the catalog into `services` /
  `service_tasks`; services missing from the file are archived, never
  deleted, and `list_services` hides them. Task enable state and
  admin-added tasks live only in SQLite; admin tasks are renumbered after
  catalog tasks so `position` stays a unique button id. Migration
  `catalog-v1` runs once: it moves admins, contacts, personal texts (with
  rules) and prefs of slides/rehearsal/coffee into broadcast/sound/kitchen
  and renames stored task names via `_move_contact` / `_move_user_text`,
  which never drop another person's binding or a user's rules. User-facing
  lists (`list_user_services`, notification state, bulk toggle) skip
  archived services.
- The legacy assembly role flow (`parse_schedule`/`parse_people`) keeps its
  own aliases: `ХКЦ` → `Ответственный`; `Ведущий`/`Ведет` → `Ведущий`;
  `Проповедует`/`Проповедет` → `Проповедник`.
- Rows without a task continue the previous task until an empty name cell,
  another task, a marker, or a date; before the first task they belong to
  the column task or the service. A row starting with `КОММЕНТАРИИ` and the
  first Sunday of another month end the sheet. A column `task` applies to
  every untasked row (also after markers); `default_task` covers untasked
  rows at the start of a date block (kitchen/order → «в смене»).
- Contacts are matched by exact (task, name); do not add name-only
  fallbacks across tasks — namesakes in one service would get each other's
  mentions and personal reminders. `_move_user_text` moves a text only
  when the user's binding moved to the target task.
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

### Rich text, mentions, and message length

- Never build HTML by hand in message text. Use markers: `code(value)` for
  code spans and `user_link(telegram_id, label)` for mentions; the renderer
  (`render_screen_chunks` / `send_telegram_screen`) escapes everything else.
- Text sent with `parse_mode=None` must go through `plain_text()` first.
- Measure Telegram limits with `visible_units()` (decoded text), not the raw
  marker string. Markers are never split across chunks; a link label longer
  than the screen is shown as plain text. Malformed markers render as
  literal text instead of raising. Templates reject `\x00`.
- Linked contacts render as `Имя (@username)` wrapped in a mention.

### Roles, /start, and command menus

- Roles: super admin (registry `super_admins`), service admin, participant;
  the configured main admin (`ADMIN_TG_ID`) additionally gets `/test_notify`
  even if the registry resolves them as a participant.
- `/start` shows inline buttons (`menu:<operation>`) for the caller's role;
  unlinked users see their Telegram ID. Command menus are set per chat via
  `BotCommandScopeChat` at startup, on `/start`, and on admin add/remove;
  "chat not found" for users who never opened the bot is logged at info.

### Responsibles overview and personal schedule

- `/service_contact` and `/service_sync` open an overview of upcoming events
  (6 weeks, via `context.load_upcoming_service_events`) with per-person link
  status; without that loader they fall back to the task list. Do not show
  Telegram IDs on these screens.
- `_load_upcoming_service_events` skips only a missing future sheet
  (`SheetNotFoundError`); malformed sheets propagate. The loaded events are
  cached for 5 minutes per process behind a lock.
- `/my_schedule` lists the user's own non-informational assignments.
- Personal service-level time uses `set_user_service_schedule`: it updates
  the first rule in place, deletes extras after copying their delivery
  records, and creates rules for tasks without one. Enabling a service with
  no rules installs `DEFAULT_PERSONAL_RULE`. `ensure_default_personal_rule`
  returns `None` on success or the reason text. Success requires an enabled
  rule on a task the user has in the current schedule; rules of tasks absent
  from the schedule do not count. Every current task without any rule gets
  the default rule, even when other tasks already have rules; disabled rules
  are never re-enabled automatically. Reasons:
  `PERSONAL_SCHEDULE_UNAVAILABLE` (schedule failed to load — stored rules
  alone are never trusted), `PERSONAL_NO_TASKS`, `PERSONAL_RULES_DISABLED`
  (every current task has rules and all are disabled). Bulk enable
  uses the short forms in `PERSONAL_PROBLEM_SHORT` because callback toasts
  are cut at 200 characters. Never mark a service enabled without a usable
  rule.
- `current_personal_tasks` caches only the Excel assignments; it re-reads
  bindings from SQLite on every call on purpose so admin changes apply
  immediately.
- Build contact-navigation callback data with
  `contact_navigation_callback`; use `contact_navigation_button` only when
  the labeled button itself is needed.
- Personal screens paginate only item rows; actions, page buttons, and
  back/close stay as separate bottom rows (`_dialog_keep_rows`).

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
- `executescript` commits any pending transaction before it runs. A
  migration script that rebuilds a table (`CREATE … _v2` / `DROP` /
  `RENAME`) must start with `BEGIN IMMEDIATE;` so it stays atomic; the
  enclosing `with connection` commits it.
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
- Telegram workflows, sessions, permissions, service menus, `/start`,
  responsibles overview, personal screens: `tests/test_handlers.py`
- Command menus by role: `tests/test_commands.py`
- Assignment formatting and mentions: `tests/test_presentation.py`
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
