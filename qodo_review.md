
- PR adds a new “app assembly” entrypoint (`bot/app.py`), a safe dry-run schedule debugger (`bot/debug_schedule.py`), a hardened filesystem helper (`bot/secure_fs.py`), a major rewrite of the Excel parser to support monthly sheets + “Списки”, hardened logging, and a large `manage.sh` process supervisor; CI workflow is also reworked to run unit tests on Ubuntu and deploy only on specific PR/push conditions.
- Overall: good security hardening intent (no-follow, permissions, escaping), but there are several correctness/reliability issues that can break deployments, parsing, and message formatting.

<QODO_CONFIRM>
{
  "identifier": "confirm-security",
  "text": "2 Security Vulnerabilities",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all security suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["sec_001_admin_message_escaping", "sec_002_credentials_temp_committed"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "sec_001_admin_message_escaping",
  "description": "**Admin notifications are HTML-escaped incorrectly, breaking ParseMode.HTML and potentially enabling formatting confusion**\n\n- Description: `_notify_admin()` wraps the entire message (including the warning prefix) in `html.escape()` and sends it while the bot default parse mode is HTML, which will render `&lt;`/`&gt;` entities literally and can hide important diagnostics; also escaping the whole string is inconsistent with other message builders that escape only interpolated fields.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/bot/notifier.py b/bot/notifier.py\n@@\n async def _notify_admin(bot: Bot, admin_id: int, text: str,\n                         debug_logger: logging.Logger) -> None:\n     try:\n-        await bot.send_message(admin_id, f\"⚠️ {text}\")\n+        await bot.send_message(admin_id, html.escape(f\"⚠️ {text}\"))\n     except Exception:  # noqa: BLE001\n         debug_logger.exception(\"Не удалось уведомить администратора\")\n```\n- Evidence: `bot/app.py` constructs admin text with exception type and message (`f\"{type(exc).__name__}: {exc}\"`), and `bot/app.py` sets `DefaultBotProperties(parse_mode=ParseMode.HTML)`, so escaping the entire message changes how it renders and can obscure the actual error content.\n- How to Fix: Escape only untrusted interpolated fields (e.g., `text`) or send admin messages with `parse_mode=None`/plain text while keeping consistent escaping strategy.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/notifier.py",
  "severity": "medium",
  "prompt": "Fix notifier._notify_admin escaping: avoid html.escape on the whole message; escape only interpolated fields or disable parse_mode for admin notifications."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "sec_002_credentials_temp_committed",
  "description": "**A credentials-like file with a BOT_TOKEN placeholder is committed to the repo**\n\n- Description: `reminder_bot_credentials.env.temp` is added to the repository and contains a `BOT_TOKEN=` line; even if it is a placeholder, this pattern increases the risk of accidentally committing real secrets later and may be picked up by tooling as a real env file.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/reminder_bot_credentials.env.temp b/reminder_bot_credentials.env.temp\nnew file mode 100644\n@@\n+BOT_TOKEN=123456789:AA...your-token-here\n+ADMIN_TG_ID=123456789\n+GROUP_CHAT_ID=-1001234567890\n+TIMEZONE=Europe/Moscow\n+YANDEX_PUBLIC_URL=https://disk.yandex.ru/i/QyNzCkuP_irvXQ\n```\n- Evidence: `.gitignore` ignores `reminder_bot_credentials.env` and `credentials.env` but does not ignore `*.env.temp`, so this file will remain tracked and can be mistaken for a deployable secret file.\n- How to Fix: Remove this file from the repo and replace it with an `.example` template (already used in README) and/or add a `.gitignore` rule for `*.env.temp`.\n",
  "filePath": "/home/pi/app/reminder_bot/src/reminder_bot_credentials.env.temp",
  "severity": "high",
  "prompt": "Remove reminder_bot_credentials.env.temp from version control and use only *.example templates; add ignore rule for *.env.temp if needed."
}
</QODO_SUGGESTION>

<QODO_CONFIRM>
{
  "identifier": "confirm-bugs",
  "text": "5 Potential bugs",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all bug suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["bug_001_deploy_condition_unreachable", "bug_002_handlers_register_signature_break", "bug_003_parse_people_overwrites_entries_with_missing_tg_id", "bug_004_excel_parser_styles_repair_zip_metadata", "bug_005_manage_sh_wait_loop_units"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "bug_001_deploy_condition_unreachable",
  "description": "**Deploy job condition is likely unreachable due to event triggers vs base_ref checks**\n\n- Description: The workflow triggers `pull_request` only for branches `test` and `main`, but the deploy job requires `github.base_ref == 'test' && github.head_ref == 'dev'`; if the PR base is `test`, the PR branch filter must include `test` as base (ok), but the workflow also removed `push` to `test`, so test environment deploys may never happen unless PRs are always dev→test and the PR event is from same repo.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/.github/workflows/deploy.yml b/.github/workflows/deploy.yml\n@@\n on:\n-  push:\n+  pull_request:\n@@\n     branches:\n       - test\n       - main\n@@\n-  pull_request:\n+  push:\n     branches:\n-      - test\n       - main\n@@\n   deploy:\n@@\n     if: >-\n       (github.event_name == 'pull_request' &&\n        github.base_ref == 'test' &&\n        github.head_ref == 'dev' &&\n        !github.event.pull_request.draft &&\n        github.event.pull_request.head.repo.full_name == github.repository) ||\n       (github.event_name == 'push' && github.ref == 'refs/heads/main')\n```\n- Evidence: The workflow no longer runs on `push` to `test`, and deploy is additionally gated to same-repo PRs only (`head.repo.full_name == github.repository`), so forks and many branch naming schemes will skip deploy entirely.\n- How to Fix: Align triggers and conditions (e.g., allow `push` to `test` or relax `head_ref` constraint) and document the required PR flow explicitly.\n",
  "filePath": "/home/pi/app/reminder_bot/src/.github/workflows/deploy.yml",
  "severity": "medium",
  "prompt": "Review deploy.yml triggers/conditions so test deploys are reachable (dev->test PRs or push-to-test), and ensure intended PR flows match the branch filters."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_002_handlers_register_signature_break",
  "description": "**Public API change in `handlers.register()` can break existing callers**\n\n- Description: `register()` now requires a `Dispatcher` argument and includes the router internally; any existing code calling `register(context)` will fail at runtime with a missing argument.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/bot/handlers.py b/bot/handlers.py\n@@\n-def register(context: Any) -> None:\n+def register(dispatcher: Dispatcher, context: Any) -> None:\n@@\n+    dispatcher.include_router(router)\n```\n- Evidence: `bot/app.py` was updated to call `register(dp, context)`, but the diff is truncated and may still contain other entrypoints/tests/import paths that call the old signature.\n- How to Fix: Provide a backward-compatible overload (accept either `(context)` or `(dispatcher, context)`) or update all call sites across the repo.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/handlers.py",
  "severity": "high",
  "prompt": "Ensure all call sites use register(dispatcher, context) or add backward-compatible signature handling."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_003_parse_people_overwrites_entries_with_missing_tg_id",
  "description": "**`parse_people()` stores entries even when Telegram ID is missing, which can overwrite valid records**\n\n- Description: The new parser always writes `people[current_role][first] = {..., 'tg_id': tg_id}` even when `tg_id` is `None`, so a later partial row with the same name can overwrite a previously valid tg_id and cause `resolve_assignments()` to treat the participant as missing Telegram ID.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/bot/excel_parser.py b/bot/excel_parser.py\n@@\n             username = _clean(cells[1].value) if len(cells) > 1 else \"\"\n             tg_id = _telegram_id(cells[2].value if len(cells) > 2 else None)\n             people[current_role][first] = {\n                 \"username\": username.lstrip(\"@\").strip(),\n                 \"tg_id\": tg_id,\n             }\n```\n- Evidence: `resolve_assignments()` treats falsy `tg_id` as missing (`if not info.get('tg_id'):`) and will append an issue/skip, so overwriting a valid `tg_id` with `None` changes notification recipients.\n- How to Fix: Only write/update a person record when `tg_id` is present, or merge records preferring non-null `tg_id`/username.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "high",
  "prompt": "Prevent parse_people from overwriting existing person entries with tg_id=None; merge records preferring non-null tg_id."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_004_excel_parser_styles_repair_zip_metadata",
  "description": "**Repacking XLSX in `_repair_known_stylesheet_issue()` may corrupt ZIP metadata/ordering and break some workbooks**\n\n- Description: The repair function rewrites the entire XLSX using `ZipFile.writestr(item, data)` with the original `ZipInfo`, but it does not preserve compression method/flags consistently and can change ordering/extra fields, which can produce unreadable files for strict consumers.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/bot/excel_parser.py b/bot/excel_parser.py\n@@\n         output = BytesIO()\n         with zipfile.ZipFile(output, \"w\") as destination:\n             for item in source.infolist():\n                 data = (\n                     repaired_stylesheet\n                     if item.filename == \"xl/styles.xml\"\n                     else source.read(item.filename)\n                 )\n                 destination.writestr(item, data)\n```\n- Evidence: `_load_workbook()` uses this repacked in-memory ZIP as the workbook source when `load_workbook()` fails with stylesheet errors, so any repack incompatibility becomes a hard failure path for schedule parsing.\n- How to Fix: Preserve original compression (`ZipFile(..., compression=...)`) and write with `writestr(item.filename, data)` while copying `compress_type`/`date_time`/`external_attr`, or use a safer patch-in-place approach.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "medium",
  "prompt": "Harden XLSX repack logic to preserve ZIP entry compression/metadata when rewriting styles.xml."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_005_manage_sh_wait_loop_units",
  "description": "**`manage.sh` PID-file wait loop uses 0.1s sleeps but counts as whole seconds, causing a 3s timeout instead of 30s**\n\n- Description: In `cmd_start`, the loop condition is `while [ \"$waited\" -lt 30 ]` but increments `waited=$((waited + 1))` after `sleep 0.1`, so it waits ~3 seconds total; slow startup (venv import, network, disk) can fail spuriously.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/manage.sh b/manage.sh\n@@\n     local pid_file_ready=0\n     local waited=0\n@@\n-    while [ \"$waited\" -lt 30 ]; do\n+    while [ \"$waited\" -lt 30 ]; do\n@@\n         sleep 0.1\n         waited=$((waited + 1))\n     done\n```\n- Evidence: The script expects the app to create the PID file (`bot/app.py` writes PID after logger/config init), and the deploy workflow installs dependencies right before start, so startup can exceed ~3 seconds on a Raspberry Pi.\n- How to Fix: Track elapsed time in tenths (e.g., `waited=$((waited + 1))` with limit 300) or use `SECONDS`/`date +%s%N` for real-time timeout.\n",
  "filePath": "/home/pi/app/reminder_bot/src/manage.sh",
  "severity": "high",
  "prompt": "Fix manage.sh cmd_start wait loop to actually wait ~30 seconds (e.g., 300 iterations with 0.1s sleep or use SECONDS-based timeout)."
}
</QODO_SUGGESTION>

<QODO_CONFIRM>
{
  "identifier": "confirm-quality",
  "text": "2 Code Quality Issues",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all quality suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["qual_001_qodo_review_committed", "qual_002_debug_schedule_unused_imports"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "qual_001_qodo_review_committed",
  "description": "**Internal review artifact `qodo_review.md` is committed and contains tool output and potentially sensitive operational details**\n\n- Description: The file includes internal analysis text, paths, and operational guidance that should not ship with production code and can confuse maintainers and CI packaging.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/qodo_review.md b/qodo_review.md\nnew file mode 100644\n@@\n+Проведу финальную проверку актуальных проблем...\n+... содержит внутренние подсказки, пути и выводы ...\n```\n- Evidence: The deploy workflow explicitly excludes `/qodo_review.md` during rsync, indicating it is not intended for runtime, but it is still tracked in git and will remain in PR history.\n- How to Fix: Remove `qodo_review.md` from the repository and keep such artifacts in PR comments or an untracked location.\n",
  "filePath": "/home/pi/app/reminder_bot/src/qodo_review.md",
  "severity": "low",
  "prompt": "Remove qodo_review.md from version control; keep review artifacts out of the repo."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "qual_002_debug_schedule_unused_imports",
  "description": "**`bot/debug_schedule.py` has unused imports and inconsistent logging setup**\n\n- Description: `logging` is imported but unused, and `setup_loggers(output_dir)` is called but only the debug logger is used; this adds noise and makes it harder to reason about what logs are produced.\n- PR Git Diff Pointer:\n```diff\ndiff --git a/bot/debug_schedule.py b/bot/debug_schedule.py\n@@\n-import logging\n@@\n-    _, logger = setup_loggers(output_dir)\n+    _, logger = setup_loggers(output_dir)\n```\n- Evidence: The module never references `logging` directly, and it prints status to stdout while also logging message previews to the debug logger, so clarity of outputs matters for safe debugging.\n- How to Fix: Remove unused imports and consider explicitly naming `interaction_logger, debug_logger = setup_loggers(...)` to avoid confusion.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/debug_schedule.py",
  "severity": "low",
  "prompt": "Clean up debug_schedule.py unused imports and clarify logger variables returned by setup_loggers()."
}
</QODO_SUGGESTION>
