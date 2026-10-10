
Added `bot/app.py` runtime entrypoint, `bot/debug_schedule.py` dry-run tool, revamped Excel parsing for monthly sheets + contacts, hardened log file creation, changed handler registration API, and introduced `manage.sh` launcher + root wrapper `reminder_bot.py`. Overall direction is good, but there are several correctness/security/reliability regressions and a few places where the new behavior likely won’t match existing deployments.

<QODO_CONFIRM>
{
  "identifier": "confirm-security",
  "text": "3 Security vulnerabilities",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all security suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["sec_001", "sec_002", "sec_003"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "sec_001",
  "description": "**PID file directory can still be attacker-controlled via symlinked `--shm-dir` path components**\n\n- Description: `_open_directory_without_symlinks()` starts from `\"/\"` and opens each component with `O_NOFOLLOW`, but `run()` creates `shm_dir` with `mkdir(..., exist_ok=True)` without validating that the resulting directory path is not a symlink chain, so an attacker who can influence `--shm-dir` could redirect PID/state/log placement.\n- PR Git Diff Pointer:\n```diff\n+async def run(args: CliArgs) -> None:\n+    shm_dir = Path(args.shm_dir)\n+    log_dir = Path(args.log_dir)\n+    shm_dir.mkdir(parents=True, exist_ok=True)\n+    log_dir.mkdir(parents=True, exist_ok=True)\n...\n+    pid_file = _pid_path(shm_dir)\n+    try:\n+        _write_pid(pid_file)\n```\n- Evidence: `run()` trusts CLI-provided directories and creates them before `_write_pid()` checks ownership/mode, and `_write_pid()` only validates the PID directory after opening it, not that the directory path itself was created safely (no `lstat` checks before `mkdir`).\n- How to Fix: Before creating/using `shm_dir` and `log_dir`, `lstat()` each path component (or use the same fd-walk approach) to ensure no symlinks and enforce ownership/mode, then create directories with safe permissions.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/app.py",
  "severity": "high",
  "prompt": "Harden bot/app.py directory handling: validate shm_dir/log_dir path components (no symlinks/..), ownership, and permissions before mkdir and PID/state/log writes."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "sec_002",
  "description": "**`debug_schedule` allows writing into arbitrary user-owned directories without symlink checks (data exposure / clobber risk)**\n\n- Description: `_secure_output_dir()` checks directory mode/owner but does not reject symlinks for `--output-dir`, so a symlinked directory owned by the user can redirect debug artifacts (xlsx + logs containing PII) into unintended locations.\n- PR Git Diff Pointer:\n```diff\n+def _secure_output_dir(path: Path) -> None:\n+    path.mkdir(parents=True, exist_ok=True, mode=0o700)\n+    info = path.lstat()\n+    if not stat.S_ISDIR(info.st_mode):\n+        raise RuntimeError(...)\n```\n- Evidence: The function uses `lstat()` but never checks `stat.S_ISLNK(info.st_mode)` and later writes `schedule-...xlsx` and logs into `output_dir`, and README explicitly warns debug logs contain Telegram IDs and message text.\n- How to Fix: Reject symlinked output directories (`S_ISLNK`) and optionally walk components to ensure no symlinks, similar to the PID directory hardening.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/debug_schedule.py",
  "severity": "medium",
  "prompt": "Harden bot/debug_schedule.py output-dir validation: reject symlinks and ensure path components are not symlinks before writing artifacts."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "sec_003",
  "description": "**Admin notification escaping is incorrect for HTML parse mode and can still allow markup injection**\n\n- Description: `_notify_admin()` wraps the entire string (including the warning symbol) in `html.escape()` and sends it while bot parse mode is HTML, which prevents intended formatting but still doesn’t guarantee safety if other send paths interpolate unescaped user-controlled fields into templates.\n- PR Git Diff Pointer:\n```diff\n async def _notify_admin(bot: Bot, admin_id: int, text: str,\n                         debug_logger: logging.Logger) -> None:\n     try:\n-        await bot.send_message(admin_id, f\"⚠️ {text}\")\n+        await bot.send_message(admin_id, html.escape(f\"⚠️ {text}\"))\n```\n- Evidence: `Bot(... DefaultBotProperties(parse_mode=ParseMode.HTML))` is set in `bot/app.py`, and `build_general_message()`/`build_personal_message()` format usernames/names into templates without escaping, so HTML injection can occur via templates or parsed Excel values.\n- How to Fix: Escape only interpolated fields (names/usernames) at formatting time (or disable HTML parse mode for these messages), and keep `_notify_admin()` consistent with the chosen escaping strategy.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/notifier.py",
  "severity": "medium",
  "prompt": "Fix HTML escaping strategy in bot/notifier.py: escape interpolated fields in templates (names/usernames) or disable HTML parse mode for messages that include untrusted data."
}
</QODO_SUGGESTION>

<QODO_CONFIRM>
{
  "identifier": "confirm-bugs",
  "text": "6 Potential bugs",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all bug suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["bug_001", "bug_002", "bug_003", "bug_004", "bug_005", "bug_006"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "bug_001",
  "description": "**PID file name mismatch between Python app and manage.sh will break start/stop/status**\n\n- Description: `bot/app.py` writes PID to `reminder_bot_bot.pid`, but `manage.sh` uses `PID_FILE=\"$SHM_DIR/reminder_bot_bot.pid\"` (same) while the README/skill mention `reminder_bot_bot.pid`; however the diff shows `_pid_path()` returns `reminder_bot_bot.pid` but manage.sh comments and earlier tooling may still expect a different name, and any mismatch will orphan processes.\n- PR Git Diff Pointer:\n```diff\n+def _pid_path(shm_dir: Path) -> Path:\n+    return shm_dir / \"reminder_bot_bot.pid\"\n```\n- Evidence: The repo previously used different entrypoints (`bot/reminder_bot.py` module) and now adds `manage.sh` plus root `reminder_bot.py`; if any existing deployment scripts still look for the old PID filename, stop/status will fail and leave the bot running.\n- How to Fix: Ensure all docs/scripts/tests consistently use the same PID filename and remove/alias any legacy PID names during transition.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/app.py",
  "severity": "medium",
  "prompt": "Audit and unify PID filename usage across bot/app.py, manage.sh, docs, and any legacy scripts; add backward-compatible cleanup for old PID names if needed."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_002",
  "description": "**`parse_people()` can crash on `first_cell.font` when running in read-only mode**\n\n- Description: In read-only mode, openpyxl returns `ReadOnlyCell` objects where style attributes may be missing or lazily loaded; accessing `first_cell.font.bold` can raise or always be `None`, causing incorrect block skipping or runtime errors.\n- PR Git Diff Pointer:\n```diff\n+            if (\n+                first\n+                and not has_extra_values\n+                and first_cell is not None\n+                and first_cell.font.bold\n+            ):\n```\n- Evidence: `_load_workbook(... read_only=True)` is used for both schedule and people parsing, and `parse_people()` iterates `worksheet.iter_rows()` returning cell objects (not `values_only=True`), so it depends on style availability.\n- How to Fix: Avoid style-based logic in read-only parsing; detect headers by content/structure only (e.g., role canonicalization + column presence) or load workbook with `read_only=False` for the contacts sheet.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "high",
  "prompt": "Fix bot/excel_parser.py parse_people(): remove reliance on cell font styles in read-only mode or switch to non-read-only loading for the contacts sheet."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_003",
  "description": "**`resolve_assignments()` signature change can break existing callers and silently drop assignments when `issues` is passed**\n\n- Description: The function now accepts optional `issues` and will `continue` (dropping that role from `result`) instead of raising, which changes downstream expectations (e.g., notifier assumes roles exist) and can cause KeyError or missing messages.\n- PR Git Diff Pointer:\n```diff\n-def resolve_assignments(schedule: dict[str, str],\n-                        people: dict[str, dict[str, dict]]\n-                        ) -> dict[str, dict]:\n+def resolve_assignments(schedule: dict[str, str],\n+                        people: dict[str, dict[str, dict]],\n+                        debug_logger: logging.Logger | None = None,\n+                        issues: list[str] | None = None,\n+                        ) -> dict[str, dict]:\n...\n+            if issues is not None:\n+                issues.append(message)\n+                continue\n```\n- Evidence: `send_assignments()` previously formatted `templates[\"general\"]` using `assignments[\"Ответственный\"][\"username\"]` etc.; now `build_general_message()` uses `.get()` fallbacks, but personal messages iterate `assignments.items()` so missing roles will skip personal notifications entirely.\n- How to Fix: Return a complete structure for all roles with explicit missing markers, or keep raising for missing `tg_id` while only warning for missing schedule roles, to avoid silently skipping intended recipients.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "medium",
  "prompt": "Stabilize resolve_assignments() behavior: avoid silently dropping roles when issues is provided; return explicit missing entries or enforce consistent downstream expectations."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_004",
  "description": "**`parse_schedule()` now raises on duplicate role rows even if one is empty/whitespace, causing unnecessary fatal runs**\n\n- Description: The duplicate detection compares `found[role] != name` before checking `name`, so a later empty name (or formatting artifact) can trigger a fatal error even though it should be ignored.\n- PR Git Diff Pointer:\n```diff\n+            role, name = task_row\n+            if role in found and found[role] != name:\n+                raise RuntimeError(\n+                    f\"Роль «{role}» указана несколько раз на дату {target:%d.%m.%Y}\"\n+                )\n+            if name:\n+                found[role] = name\n```\n- Evidence: `_parse_task_row()` can return `(role, \"\")` when the role header is present but the assignee cell is empty, and the loop processes all rows until the next date marker.\n- How to Fix: Only treat as duplicate when `name` is non-empty (and different), or prefer “first non-empty wins” with a warning.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "medium",
  "prompt": "Adjust parse_schedule() duplicate-role logic to ignore empty names and avoid fatal errors on formatting artifacts."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_005",
  "description": "**`manage.sh` writes PID file itself, racing with Python app PID creation and causing inconsistent state**\n\n- Description: `manage.sh start` writes `$PID_FILE` after launching the process, but `bot/app.py` also creates the PID file exclusively (`O_EXCL`), so one of them will fail depending on timing and leave either the script or the app thinking startup failed.\n- PR Git Diff Pointer:\n```diff\n+    nohup \"$PYTHON_EXEC\" -u \"$BOT_SCRIPT\" \\\n+        --shm-dir \"$SHM_DIR\" \\\n+        --log-dir \"$LOG_DIR\" \\\n+        > \"$NOHUP_LOG\" 2>&1 &\n+    local new_pid=$!\n...\n+    echo \"$new_pid:$new_starttime\" > \"$PID_FILE\"\n```\n- Evidence: `bot/app.py` calls `_write_pid(pid_file)` with `os.O_EXCL`, and `manage.sh` unconditionally creates the same file path, so the two components are not coordinated.\n- How to Fix: Make PID ownership single-source-of-truth: either let the Python app manage the PID file and have manage.sh only read it, or disable PID creation in the app when launched under manage.sh.\n",
  "filePath": "/home/pi/app/reminder_bot/src/manage.sh",
  "severity": "high",
  "prompt": "Fix PID file ownership conflict between manage.sh and bot/app.py: choose one component to create/remove PID files and update the other to only read/validate."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_006",
  "description": "**`debug_schedule.run_check()` sets loggers to output_dir but does not ensure log file permissions after creation**\n\n- Description: It calls `setup_loggers(output_dir)` and then `os.chmod(path, 0o600)` for the xlsx, but does not enforce `0o600` on the created log files, so logs may inherit umask and become group-readable.\n- PR Git Diff Pointer:\n```diff\n+    _, logger = setup_loggers(output_dir)\n...\n+    os.chmod(path, 0o600)\n```\n- Evidence: README/skill explicitly state debug logs contain names/usernames/Telegram IDs and must be private, and `setup_loggers()` creates files but relies on `_file_handler()` only in the updated logger module; if `setup_loggers()` is bypassed or changed, permissions may drift.\n- How to Fix: After `setup_loggers()`, explicitly chmod the expected log files in `output_dir` to `0o600` (or have `setup_loggers()` guarantee permissions for any directory).\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/debug_schedule.py",
  "severity": "medium",
  "prompt": "Ensure debug_schedule enforces 0600 permissions on created log files in output_dir (or centralize this guarantee in setup_loggers)."
}
</QODO_SUGGESTION>

<QODO_CONFIRM>
{
  "identifier": "confirm-quality",
  "text": "3 Code quality / reliability issues",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all quality suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["qual_001", "qual_002", "qual_003"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "qual_001",
  "description": "**Shutdown sequence may leave polling running; relies on `finally` + session close side effects**\n\n- Description: `perform_shutdown()` shuts down the scheduler and closes the bot session, but does not explicitly stop/cancel polling, so shutdown behavior depends on aiogram internals and can hang under network stalls.\n- PR Git Diff Pointer:\n```diff\n+    async def perform_shutdown() -> None:\n+        scheduler.shutdown(wait=False)\n+        await bot.session.close()\n+        pid_file = _pid_path(shm_dir)\n+        if pid_file.exists():\n+            pid_file.unlink()\n```\n- Evidence: `run()` awaits `dp.start_polling(bot, close_bot_session=False)` and only triggers shutdown in `finally`, without cancelling the polling task or calling a dispatcher stop API.\n- How to Fix: Run polling in a task and cancel it during shutdown (or use aiogram-supported shutdown hooks) before closing the session.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/app.py",
  "severity": "low",
  "prompt": "Improve bot/app.py shutdown: explicitly stop/cancel polling before closing bot session and removing PID file."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "qual_002",
  "description": "**`setup_loggers()` closes handlers from both loggers but doesn’t remove them from other logger instances (risk of closing shared streams)**\n\n- Description: It collects handlers from `interaction` and `debug` and calls `handler.close()` before clearing, which can close streams that might be shared/reused elsewhere if other code attached the same handler instance.\n- PR Git Diff Pointer:\n```diff\n+    old_handlers = set(interaction.handlers)\n+    debug = logging.getLogger(\"reminder_bot.debug\")\n+    old_handlers.update(debug.handlers)\n+    for handler in old_handlers:\n+        handler.close()\n```\n- Evidence: `_ClosingStreamHandler.close()` closes any stream not equal to `sys.stdout`/`sys.stderr`, so if a handler was shared across loggers (possible in Python logging), closing it here can break other logging.\n- How to Fix: Only close handlers that were created by `setup_loggers()` (track them), or remove handlers from loggers before closing to avoid affecting shared handler instances.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/logger.py",
  "severity": "low",
  "prompt": "Make setup_loggers() safer: only close handlers it owns or detach handlers before closing to avoid closing shared streams."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "qual_003",
  "description": "**`manage.sh` uses `tail -f` without validating log file safety (symlink/hardlink) before following**\n\n- Description: `cmd_logs`/`cmd_debug_logs` follow files directly, so if `$LOG_DIR` is misconfigured or compromised, `tail -f` could follow a symlink to sensitive files and leak contents to the terminal/session.\n- PR Git Diff Pointer:\n```diff\n+    if [ -f \"$LOG_FILE\" ]; then\n+        if [ -n \"$follow_flag\" ]; then\n+            tail -n 10 -f \"$LOG_FILE\"\n```\n- Evidence: The script has safety helpers (`secure_log_file_for_backup`, `has_symlink_path_component`) but does not apply them before reading/following logs, and `LOG_DIR` is configurable via environment.\n- How to Fix: Reuse `secure_log_file_for_backup`/`is_safe_backup_file`-style checks (regular file, owned, no symlink, nlink=1, mode 600) before tailing.\n",
  "filePath": "/home/pi/app/reminder_bot/src/manage.sh",
  "severity": "low",
  "prompt": "Harden manage.sh logs/debug-logs: validate log file is a safe regular file (no symlink/hardlink, owned, 0600) before tailing/following."
}
</QODO_SUGGESTION>Added `bot/app.py` runtime entrypoint, `bot/debug_schedule.py` dry-run tool, revamped Excel parsing for monthly sheets + contacts, hardened log file creation, changed handler registration API, and introduced `manage.sh` launcher + root wrapper `reminder_bot.py`. Overall direction is good, but there are several correctness/security/reliability regressions and a few places where the new behavior likely won’t match existing deployments.

<QODO_CONFIRM>
{
  "identifier": "confirm-security",
  "text": "2 Security vulnerabilities",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all security suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["sec_001", "sec_002"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "sec_001",
  "description": "**`debug_schedule` output directory validation does not reject symlinks (PII/log redirection risk)**\n\n- Description: `_secure_output_dir()` checks owner/mode but allows `--output-dir` to be a symlink, so debug artifacts (xlsx + logs containing Telegram IDs and message text) can be redirected to unintended locations.\n- PR Git Diff Pointer:\n```diff\n+def _secure_output_dir(path: Path) -> None:\n+    path.mkdir(parents=True, exist_ok=True, mode=0o700)\n+    info = path.lstat()\n+    if not stat.S_ISDIR(info.st_mode):\n+        raise RuntimeError(f\"Каталог отладки не является обычным каталогом: {path}\")\n```\n- Evidence: README/skill explicitly state debug logs contain names/usernames/Telegram IDs and must be private, and `run_check()` writes both the downloaded workbook and logs into `output_dir`.\n- How to Fix: Reject symlinked directories (`stat.S_ISLNK(info.st_mode)`) and optionally walk path components to ensure none are symlinks.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/debug_schedule.py",
  "severity": "medium",
  "prompt": "Harden bot/debug_schedule.py: reject symlinked output-dir and ensure path components are not symlinks before writing artifacts."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "sec_002",
  "description": "**HTML parse mode + unescaped template interpolation can allow markup injection in group/personal messages**\n\n- Description: The bot is configured with `ParseMode.HTML`, but `build_general_message()` and `build_personal_message()` interpolate `username`/`name` into templates without escaping, so values coming from Excel can inject HTML markup into Telegram messages.\n- PR Git Diff Pointer:\n```diff\n+    bot = Bot(\n+        token=credentials.bot_token,\n+        default=DefaultBotProperties(parse_mode=ParseMode.HTML),\n+    )\n```\n```diff\n+def build_personal_message(\n+    role: str,\n+    info: dict,\n+    templates: dict,\n+    sunday_str: str,\n+) -> str:\n+    return templates[\"roles\"][role].format(\n+        name=info[\"name\"], sunday_date=sunday_str,\n+    )\n```\n- Evidence: `parse_schedule()` and `parse_people()` source `name`/`username` from workbook cells, and those values flow into `send_assignments()` via `build_general_message()`/`build_personal_message()`.\n- How to Fix: Escape interpolated fields (`html.escape(name/username)`) when formatting messages, or disable HTML parse mode for these messages.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/notifier.py",
  "severity": "high",
  "prompt": "Fix bot/notifier.py message formatting under ParseMode.HTML: escape untrusted fields (names/usernames) or switch to plain text parse mode."
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
  "suggestionIdentifiers": ["bug_001", "bug_002", "bug_003", "bug_004", "bug_005"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "bug_001",
  "description": "**PID file ownership conflict: manage.sh writes PID file but app also creates it exclusively**\n\n- Description: `manage.sh start` writes `$PID_FILE` after launching the process, while `bot/app.py` creates the same PID file with `O_EXCL`; depending on timing, either the app will fail with `FileExistsError` or the script will overwrite the app’s PID semantics.\n- PR Git Diff Pointer:\n```diff\n+    try:\n+        _write_pid(pid_file)\n+    except FileExistsError as exc:\n+        ...\n+        raise FileExistsError(message) from exc\n```\n```diff\n+    echo \"$new_pid:$new_starttime\" > \"$PID_FILE\"\n```\n- Evidence: `bot/app.py` `_write_pid()` uses `os.O_EXCL` and writes `pid:starttime`, and `manage.sh` unconditionally writes the same format to the same path.\n- How to Fix: Make PID creation single-source-of-truth (either only Python app creates/removes PID, or only manage.sh does) and update the other component to only read/validate.\n",
  "filePath": "/home/pi/app/reminder_bot/src/manage.sh",
  "severity": "high",
  "prompt": "Resolve PID file race between manage.sh and bot/app.py by choosing one component to create/remove PID files and updating the other to only read/validate."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_002",
  "description": "**`parse_people()` may crash or misbehave due to style access (`first_cell.font.bold`) in read-only mode**\n\n- Description: The workbook is loaded with `read_only=True`, but `parse_people()` accesses `first_cell.font.bold`; in read-only mode style info may be unavailable or expensive, causing exceptions or incorrect header detection.\n- PR Git Diff Pointer:\n```diff\n+    workbook = _load_workbook(path, debug_logger)\n...\n+        for raw_row in worksheet.iter_rows():\n+            cells = list(raw_row)\n...\n+            if (\n+                first\n+                and not has_extra_values\n+                and first_cell is not None\n+                and first_cell.font.bold\n+            ):\n```\n- Evidence: `_load_workbook()` always calls `load_workbook(... read_only=True)`, and `parse_people()` iterates cells (not `values_only=True`) specifically to read style.\n- How to Fix: Avoid style-based logic in read-only parsing (use content/column heuristics), or load the contacts sheet with `read_only=False`.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "high",
  "prompt": "Fix parse_people() in bot/excel_parser.py: remove reliance on font.bold in read-only mode or load the contacts sheet non-read-only."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_003",
  "description": "**`parse_schedule()` duplicate-role detection can raise on empty/formatting rows**\n\n- Description: The code raises if the same role appears again with a different `name`, but it checks duplicates before verifying `name` is non-empty, so a later empty assignment row can trigger a fatal error.\n- PR Git Diff Pointer:\n```diff\n+            role, name = task_row\n+            if role in found and found[role] != name:\n+                raise RuntimeError(\n+                    f\"Роль «{role}» указана несколько раз на дату {target:%d.%m.%Y}\"\n+                )\n+            if name:\n+                found[role] = name\n```\n- Evidence: `_parse_task_row()` returns `(role, \"\")` when the role header exists but the assignee cell is empty, and the loop processes all rows until the next date marker.\n- How to Fix: Only treat as duplicate when `name` is non-empty (and different), or keep first non-empty value and log a warning.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "medium",
  "prompt": "Adjust bot/excel_parser.py parse_schedule() duplicate-role logic to ignore empty names and avoid fatal errors on formatting artifacts."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_004",
  "description": "**`resolve_assignments()` behavior change can silently drop recipients when `issues` is provided**\n\n- Description: When `issues` is passed, missing people or missing `tg_id` causes `continue`, so that role is omitted from `assignments` and personal messages are skipped without a hard failure.\n- PR Git Diff Pointer:\n```diff\n+            if issues is not None:\n+                issues.append(message)\n+                continue\n```\n- Evidence: `send_assignments()` iterates `for role, info in assignments.items()` for personal messages, so omitted roles will never receive a DM even if the schedule had an assignee name.\n- How to Fix: Return explicit entries for scheduled roles with missing contact data (e.g., `tg_id=None`) and let notifier decide what to send, or keep raising for missing `tg_id` if that should be fatal.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/excel_parser.py",
  "severity": "medium",
  "prompt": "Stabilize resolve_assignments() output: avoid silently dropping roles when issues is provided; return explicit missing-contact entries or enforce consistent fatal/non-fatal rules."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "bug_005",
  "description": "**`_notify_admin()` escapes the whole message, but other message paths remain unescaped under HTML parse mode**\n\n- Description: Escaping only admin notifications creates inconsistent behavior: admin messages become literal text while group/personal messages still interpolate unescaped fields, so the change doesn’t actually address injection risk and may reduce readability.\n- PR Git Diff Pointer:\n```diff\n-        await bot.send_message(admin_id, f\"⚠️ {text}\")\n+        await bot.send_message(admin_id, html.escape(f\"⚠️ {text}\"))\n```\n- Evidence: `bot/app.py` sets `ParseMode.HTML` globally, and `build_general_message()`/`build_personal_message()` format unescaped `name`/`username` into templates.\n- How to Fix: Apply a consistent escaping policy across all outgoing messages (escape interpolated fields everywhere, or disable HTML parse mode for all messages).\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/notifier.py",
  "severity": "medium",
  "prompt": "Unify escaping policy in bot/notifier.py: escape interpolated fields for admin/group/personal messages consistently (or disable HTML parse mode)."
}
</QODO_SUGGESTION>

<QODO_CONFIRM>
{
  "identifier": "confirm-quality",
  "text": "2 Code quality / reliability issues",
  "ctaText": "Resolve all",
  "doneCtaText": "✓ All resolved",
  "prompt": "Resolve all quality suggestions",
  "type": "resolve_all",
  "suggestionIdentifiers": ["qual_001", "qual_002"]
}
</QODO_CONFIRM>

<QODO_SUGGESTION>
{
  "identifier": "qual_001",
  "description": "**Shutdown sequence doesn’t explicitly stop polling; may hang depending on aiogram internals**\n\n- Description: `perform_shutdown()` shuts down the scheduler and closes the bot session, but does not cancel/stop `dp.start_polling`, so shutdown relies on side effects and can delay exit.\n- PR Git Diff Pointer:\n```diff\n+    async def perform_shutdown() -> None:\n+        scheduler.shutdown(wait=False)\n+        await bot.session.close()\n```\n- Evidence: `run()` awaits `dp.start_polling(bot, close_bot_session=False)` and only calls `shutdown()` in `finally`, without cancelling the polling task.\n- How to Fix: Run polling in a task and cancel it during shutdown (or use aiogram shutdown hooks) before closing the session.\n",
  "filePath": "/home/pi/app/reminder_bot/src/bot/app.py",
  "severity": "low",
  "prompt": "Improve bot/app.py shutdown: explicitly stop/cancel polling before closing bot session."
}
</QODO_SUGGESTION>

<QODO_SUGGESTION>
{
  "identifier": "qual_002",
  "description": "**`manage.sh` tails logs without safety checks even though LOG_DIR is configurable**\n\n- Description: `logs --follow` and `debug-logs --follow` call `tail -f` on `$LOG_FILE`/`$DEBUG_LOG_FILE` without verifying they are safe regular files (no symlink/hardlink), so a misconfigured or compromised `LOG_DIR` can leak arbitrary file contents.\n- PR Git Diff Pointer:\n```diff\n+    if [ -f \"$LOG_FILE\" ]; then\n+        if [ -n \"$follow_flag\" ]; then\n+            tail -n 10 -f \"$LOG_FILE\"\n```\n- Evidence: The script already has safety helpers (`secure_log_file_for_backup`, `has_symlink_path_component`) but does not apply them before reading/following logs, and `LOG_DIR` can be overridden via environment.\n- How to Fix: Validate log files (regular, owned, mode 600, nlink=1, not symlink) before tailing/following.\n",
  "filePath": "/home/pi/app/reminder_bot/src/manage.sh",
  "severity": "low",
  "prompt": "Harden manage.sh logs/debug-logs: validate log files are safe regular files (no symlink/hardlink, owned, 0600) before tailing."
}
</QODO_SUGGESTION>
