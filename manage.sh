#!/bin/bash
# @reboot        /home/pi/app/reminder_bot/prod/reminder_bot_manage.sh start
#0 * * * *      /home/pi/app/reminder_bot/prod/reminder_bot_manage.sh start > /dev/null 2>&1
#30 23 * * *    /home/pi/app/reminder_bot/prod/reminder_bot_manage.sh backup > /dev/null 2>&1

# reminder_bot_manage.sh
# Управляющий скрипт для Telegram-бота напоминаний (reminder_bot.py).
# Приведён в соответствие с ТЗ (tz.txt):
#   - основной файл: reminder_bot.py
#   - запуск: python3 reminder_bot.py
#   - ОЗУ: /dev/shm/reminder_bot/{test|prod}/
#   - PID: /dev/shm/reminder_bot/{test|prod}/reminder_bot_bot.pid
#   - Логи: reminder_bot_interaction.log (INFO), reminder_bot_debug.log (DEBUG)
#   - Команды: start | stop | restart | status | backup | logs | debug-logs | clear-logs
#   - stop делает backup перед kill; start восстанавливает лог из backup при чистой ОЗУ.
#   - status печатает "БОТ РАБОТАЕТ" для CI-проверок.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="$(basename "$PROJECT_DIR")"          # test | prod
BOT_SCRIPT="$PROJECT_DIR/reminder_bot.py"

# --------------------------------------------
# Определяем Python (/home/pi/.venv приоритетно)
# --------------------------------------------
if [ -x "/home/pi/.venv/bin/python3" ]; then
    PYTHON_EXEC="/home/pi/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
    PYTHON_EXEC="$PROJECT_DIR/.venv/bin/python3"
elif [ -x "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON_EXEC="$PROJECT_DIR/venv/bin/python3"
elif [ -x "$HOME/.venv/bin/python3" ]; then
    PYTHON_EXEC="$HOME/.venv/bin/python3"
else
    PYTHON_EXEC="python3"
fi

# --------------------------------------------
# Пути ОЗУ (RAM-диск) и логов
# --------------------------------------------
SHM_BASE="${SHM_BASE:-/dev/shm/reminder_bot}"
SHM_DIR="${SHM_DIR:-$SHM_BASE/$ENV_NAME}"
LOG_DIR="${LOG_DIR:-$SHM_DIR}"

PID_FILE="$SHM_DIR/reminder_bot_bot.pid"
LOG_FILE="$LOG_DIR/reminder_bot_interaction.log"
DEBUG_LOG_FILE="$LOG_DIR/reminder_bot_debug.log"
NOHUP_LOG="$LOG_DIR/sys_nohup.log"

# Персистентные бэкапы логов на SD-карте
BACKUP_DIR="$PROJECT_DIR/logs_backup"

ensure_backup_dir() {
    if [ -L "$BACKUP_DIR" ]; then
        echo "❌ Каталог бэкапов не должен быть символической ссылкой: $BACKUP_DIR" >&2
        return 1
    fi
    mkdir -p "$BACKUP_DIR" || return 1
    if [ ! -d "$BACKUP_DIR" ] || [ ! -O "$BACKUP_DIR" ]; then
        echo "❌ Каталог бэкапов должен принадлежать текущему пользователю: $BACKUP_DIR" >&2
        return 1
    fi
    chmod 700 "$BACKUP_DIR"
}

is_safe_backup_file() {
    local file=$1
    local mode
    local links
    if [ ! -f "$file" ] || [ -L "$file" ] || [ ! -O "$file" ]; then
        return 1
    fi
    mode=$(stat -c '%a' -- "$file") || return 1
    links=$(stat -c '%h' -- "$file") || return 1
    [[ "$mode" =~ ^[0-7]+$ ]] || return 1
    [ "$links" -eq 1 ] || return 1
    (( (8#$mode & 022) == 0 ))
}

has_symlink_path_component() {
    local path=$1
    local current="/"
    local component
    local -a components=()

    if [[ "$path" != /* ]]; then
        current=$PWD
    fi
    IFS='/' read -r -a components <<< "$path"
    for component in "${components[@]}"; do
        case "$component" in
            ""|".")
                continue
                ;;
            "..")
                return 0
                ;;
        esac
        current="${current%/}/$component"
        [ -L "$current" ] && return 0
    done
    return 1
}

secure_log_file_for_backup() {
    local file=$1
    local links
    if [ ! -f "$file" ] || [ -L "$file" ] || [ ! -O "$file" ]; then
        return 1
    fi
    links=$(stat -c '%h' -- "$file") || return 1
    [ "$links" -eq 1 ] || return 1
    chmod 600 "$file"
}

secure_private_file() {
    local file=$1
    local links
    if has_symlink_path_component "$file" ||
       [ ! -f "$file" ] || [ -L "$file" ] || [ ! -O "$file" ]; then
        return 1
    fi
    links=$(stat -c '%h' -- "$file") || return 1
    [ "$links" -eq 1 ] || return 1
    chmod 600 "$file"
}

is_project_pythonpath() {
    local pid=$1
    local environment_entry
    local pythonpath
    local -a environment=()

    mapfile -d '' -t environment < "/proc/$pid/environ" 2>/dev/null || return 1
    for environment_entry in "${environment[@]}"; do
        if [[ "$environment_entry" == PYTHONPATH=* ]]; then
            pythonpath=${environment_entry#PYTHONPATH=}
            case ":$pythonpath:" in
                *":$PROJECT_DIR:"*)
                    return 0
                    ;;
            esac
        fi
    done
    return 1
}

is_legacy_bot_process() {
    local pid=$1
    local cwd
    local entrypoint_index=1
    local -a argv=()

    mapfile -d '' -t argv < "/proc/$pid/cmdline" 2>/dev/null || return 1
    [ "${#argv[@]}" -gt 0 ] || return 1
    [[ "${argv[0]##*/}" == python* ]] || return 1
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || return 1

    while [ "$entrypoint_index" -lt "${#argv[@]}" ]; do
        case "${argv[entrypoint_index]}" in
            -m)
                [ "${argv[entrypoint_index + 1]:-}" = "bot.reminder_bot" ] &&
                    { [ "$cwd" = "$PROJECT_DIR" ] || is_project_pythonpath "$pid"; }
                return $?
                ;;
            -X|-W)
                entrypoint_index=$((entrypoint_index + 2))
                ;;
            -X*|-W*)
                entrypoint_index=$((entrypoint_index + 1))
                ;;
            -B|-E|-I|-O|-OO|-s|-S|-u)
                entrypoint_index=$((entrypoint_index + 1))
                ;;
            -*)
                return 1
                ;;
            *)
                break
                ;;
        esac
    done

    [ "$entrypoint_index" -lt "${#argv[@]}" ] || return 1
    case "${argv[entrypoint_index]}" in
        "$BOT_SCRIPT"|"$PROJECT_DIR/bot/reminder_bot.py")
            return 0
            ;;
        reminder_bot.py|./reminder_bot.py|bot/reminder_bot.py|./bot/reminder_bot.py)
            [ "$cwd" = "$PROJECT_DIR" ] && return 0
            ;;
    esac
    return 1
}

find_legacy_pids() {
    local cmdline_file pid
    for cmdline_file in /proc/[0-9]*/cmdline; do
        pid=${cmdline_file#/proc/}
        pid=${pid%/cmdline}
        if is_legacy_bot_process "$pid"; then
            printf '%s\n' "$pid"
        fi
    done
}

# --------------------------------------------
# Функция получения времени старта процесса (тик из /proc/<pid>/stat)
# --------------------------------------------
get_process_starttime() {
    local pid=$1
    if [ ! -f "/proc/$pid/stat" ]; then
        echo ""
        return 1
    fi
    awk '{print $22}' "/proc/$pid/stat" 2>/dev/null
}

# --------------------------------------------
# Функция получения состояния процесса (R, S, Z, T и т.д.)
# --------------------------------------------
get_process_state() {
    local pid=$1
    ps -o state= -p "$pid" 2>/dev/null
}

# --------------------------------------------
# Проверка, что процесс с данным PID и starttime жив и не зомби
# --------------------------------------------
is_valid_process() {
    local pid=$1
    local saved_starttime=$2
    if [ -z "$pid" ] || [ -z "$saved_starttime" ]; then
        return 1
    fi
    local current_starttime
    current_starttime=$(get_process_starttime "$pid")
    if [ -z "$current_starttime" ]; then
        return 1
    fi
    # starttime должен совпадать (защита от переиспользования PID)
    [ "$current_starttime" != "$saved_starttime" ] && return 1

    # Процесс не должен быть зомби
    local state
    state=$(get_process_state "$pid")
    if [ -z "$state" ] || [ "$state" == "Z" ]; then
        return 1
    fi
    return 0
}

# --------------------------------------------
# Мягкое завершение с ожиданием и SIGKILL-фолбэком
# --------------------------------------------
kill_with_wait() {
    local pid=$1
    local expected_starttime=$2
    local timeout=${3:-10}
    local name=${4:-"процесс"}

    if ! is_valid_process "$pid" "$expected_starttime"; then
        echo "⚠️ Процесс $pid не соответствует ожидаемому (starttime не совпадает или зомби). Пропускаем."
        return 0
    fi

    echo "⏳ Остановка $name (PID $pid)..."
    kill -15 "$pid" 2>/dev/null

    local waited=0
    while is_valid_process "$pid" "$expected_starttime"; do
        if [ $waited -ge $timeout ]; then
            echo "⚠️ $name не завершился за ${timeout}с, принудительно завершаем..."
            kill -9 "$pid" 2>/dev/null
            local kill_waited=0
            while is_valid_process "$pid" "$expected_starttime" && [ $kill_waited -lt 5 ]; do
                sleep 1
                kill_waited=$((kill_waited + 1))
            done
            if is_valid_process "$pid" "$expected_starttime"; then
                echo "❌ Не удалось завершить $name (PID $pid)"
                return 1
            else
                echo "✅ $name принудительно завершён"
                return 0
            fi
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo "✅ $name завершён"
    return 0
}

# --------------------------------------------
# Остановка legacy-процесса (запущенного без PID-файла)
# --------------------------------------------
stop_legacy_process() {
    local pid=$1
    local name=${2:-"legacy-процесс"}

    local original_starttime
    original_starttime=$(get_process_starttime "$pid")
    if [ -z "$original_starttime" ]; then
        echo "ℹ️ $name (PID $pid) уже не активен"
        return 0
    fi

    echo "⏳ Остановка $name (PID $pid)..."
    kill -15 "$pid" 2>/dev/null

    local waited=0
    while [ $waited -lt 5 ]; do
        if ! is_valid_process "$pid" "$original_starttime"; then
            echo "✅ $name (PID $pid) завершён"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if is_valid_process "$pid" "$original_starttime"; then
        echo "⚠️ $name не завершился gracefully, принудительно завершаем..."
        kill -9 "$pid" 2>/dev/null
        local kill_waited=0
        while [ $kill_waited -lt 5 ]; do
            if ! is_valid_process "$pid" "$original_starttime"; then
                echo "✅ $name (PID $pid) принудительно завершён"
                return 0
            fi
            sleep 1
            kill_waited=$((kill_waited + 1))
        done
        if is_valid_process "$pid" "$original_starttime"; then
            echo "❌ Не удалось завершить $name (PID $pid)"
            return 1
        else
            echo "✅ $name (PID $pid) завершён"
            return 0
        fi
    else
        echo "✅ $name (PID $pid) завершён"
        return 0
    fi
}

# --------------------------------------------
# Бэкап лога взаимодействий на SD-карту
# --------------------------------------------
do_backup() {
    ensure_backup_dir || return 1
    if [ -s "$LOG_FILE" ]; then
        if ! secure_log_file_for_backup "$LOG_FILE" ||
           ! is_safe_backup_file "$LOG_FILE"; then
            echo "❌ Файл лога небезопасен для резервного копирования: $LOG_FILE" >&2
            return 1
        fi
        local backup_file="$BACKUP_DIR/reminder_bot_interaction_$(date +%F).log"
        local backup_tmp
        backup_tmp=$(mktemp "$BACKUP_DIR/.reminder_bot_interaction.XXXXXX") || return 1
        if ! cp -- "$LOG_FILE" "$backup_tmp" ||
           ! chmod 600 "$backup_tmp" ||
           ! mv -fT -- "$backup_tmp" "$backup_file"; then
            rm -f -- "$backup_tmp"
            return 1
        fi
        echo "💾 Бэкап создан: $backup_file"
    else
        echo "ℹ️ Нет данных для бэкапа ($LOG_FILE пуст или отсутствует)."
    fi
}

# --------------------------------------------
# Восстановление последнего лога из бэкапа (после перезагрузки Pi)
# --------------------------------------------
restore_latest_log() {
    if [ -s "$LOG_FILE" ]; then
        return 0
    fi
    ensure_backup_dir || return 1
    local latest=""
    local candidate
    local found_backup=0
    for candidate in "$BACKUP_DIR"/reminder_bot_interaction_*.log; do
        if [ ! -e "$candidate" ] && [ ! -L "$candidate" ]; then
            continue
        fi
        found_backup=1
        if ! is_safe_backup_file "$candidate"; then
            echo "⚠️ Пропускаем небезопасный файл бэкапа: $candidate" >&2
            continue
        fi
        if [ -z "$latest" ] || [ "$candidate" -nt "$latest" ]; then
            latest=$candidate
        fi
    done
    if [ -n "$latest" ]; then
        local restore_tmp
        restore_tmp=$(mktemp "$LOG_DIR/.reminder_bot_restore.XXXXXX") || return 1
        if ! install -m 600 -- "$latest" "$restore_tmp" ||
           ! mv -fT -- "$restore_tmp" "$LOG_FILE"; then
            rm -f -- "$restore_tmp"
            return 1
        fi
        echo "♻️ Восстановлен interaction.log из $latest"
    elif [ "$found_backup" -eq 1 ]; then
        echo "❌ Не найден безопасный файл для восстановления лога." >&2
        return 1
    fi
}

# --------------------------------------------
# Команда start
# --------------------------------------------
cmd_start() {
    echo "🚀 Запуск бота ($ENV_NAME)..."

    if has_symlink_path_component "$SHM_DIR"; then
        echo "❌ Путь к каталогу PID-файла не должен содержать символические ссылки или '..': $SHM_DIR" >&2
        exit 1
    fi
    mkdir -p "$SHM_DIR" || exit 1
    if has_symlink_path_component "$SHM_DIR"; then
        echo "❌ Путь к каталогу PID-файла изменился или содержит символическую ссылку: $SHM_DIR" >&2
        exit 1
    fi
    if has_symlink_path_component "$LOG_DIR"; then
        echo "❌ Путь к каталогу логов не должен содержать символические ссылки или '..': $LOG_DIR" >&2
        exit 1
    fi
    mkdir -p "$LOG_DIR" || exit 1
    if has_symlink_path_component "$LOG_DIR" || [ ! -d "$LOG_DIR" ] || [ ! -O "$LOG_DIR" ]; then
        echo "❌ Каталог логов должен быть обычным каталогом текущего пользователя: $LOG_DIR" >&2
        exit 1
    fi
    chmod 700 "$SHM_DIR" || exit 1
    chmod 700 "$LOG_DIR" || exit 1
    ensure_backup_dir || exit 1

    if [ -e "$NOHUP_LOG" ] || [ -L "$NOHUP_LOG" ]; then
        if ! secure_private_file "$NOHUP_LOG"; then
            echo "❌ Файл системного лога небезопасен: $NOHUP_LOG" >&2
            exit 1
        fi
        : > "$NOHUP_LOG" || exit 1
    else
        (umask 077; set -o noclobber; : > "$NOHUP_LOG") || {
            echo "❌ Не удалось безопасно создать системный лог: $NOHUP_LOG" >&2
            exit 1
        }
    fi

    # Проверка активного PID-файла
    if [ -f "$PID_FILE" ] || [ -L "$PID_FILE" ]; then
        local saved_pid saved_starttime
        if ! secure_private_file "$PID_FILE"; then
            echo "❌ PID-файл небезопасен: $PID_FILE" >&2
            exit 1
        fi
        saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if is_valid_process "$saved_pid" "$saved_starttime"; then
            echo "⚠️ Бот уже запущен (PID $saved_pid)!"
            exit 1
        else
            echo "⚠️ Найден устаревший PID-файл. Удаляем."
            rm -f "$PID_FILE"
        fi
    fi

    # Восстанавливаем последний лог из бэкапа, если ОЗУ чистая
    restore_latest_log || exit 1

    if [ ! -f "$BOT_SCRIPT" ]; then
        echo "❌ Не найден основной файл бота: $BOT_SCRIPT"
        exit 1
    fi

    cd "$PROJECT_DIR" || exit 1

    PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        RUNNER_TRACKING_ID="${RUNNER_TRACKING_ID:-0}" \
        nohup "$PYTHON_EXEC" -u "$BOT_SCRIPT" \
        --shm-dir "$SHM_DIR" \
        --log-dir "$LOG_DIR" \
        >> "$NOHUP_LOG" 2>&1 &
    local new_pid=$!

    local new_starttime
    new_starttime=$(get_process_starttime "$new_pid")
    if [ -z "$new_starttime" ]; then
        echo "❌ Не удалось получить время старта процесса $new_pid"
        exit 1
    fi

    local pid_file_ready=0
    local waited=0
    local saved_pid saved_starttime
    while [ "$waited" -lt 30 ]; do
        if [ -f "$PID_FILE" ] || [ -L "$PID_FILE" ]; then
            if secure_private_file "$PID_FILE"; then
                saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
                saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
                if [ "$saved_pid" = "$new_pid" ] &&
                   [ "$saved_starttime" = "$new_starttime" ]; then
                    pid_file_ready=1
                    break
                fi
            fi
        fi
        is_valid_process "$new_pid" "$new_starttime" || break
        sleep 0.1
        waited=$((waited + 1))
    done

    if [ "$pid_file_ready" -eq 1 ] &&
       is_valid_process "$new_pid" "$new_starttime"; then
        echo "✅ Бот успешно запущен (PID $new_pid) [$ENV_NAME]."
        echo "📄 interaction.log: $LOG_FILE"
        echo "📄 debug.log:       $DEBUG_LOG_FILE"
        exit 0
    else
        echo "❌ Ошибка старта! Проверьте $NOHUP_LOG"
        if is_valid_process "$new_pid" "$new_starttime"; then
            kill_with_wait "$new_pid" "$new_starttime" 3 "процесс с неудачным запуском"
        fi
        exit 1
    fi
}

# --------------------------------------------
# Команда stop (с предварительным backup)
# --------------------------------------------
cmd_stop() {
    echo "🛑 Остановка бота ($ENV_NAME)..."

    # 1. Бэкап перед остановкой
    do_backup

    # 2. Остановка по PID-файлу
    if [ -f "$PID_FILE" ] || [ -L "$PID_FILE" ]; then
        local saved_pid saved_starttime
        if ! secure_private_file "$PID_FILE"; then
            echo "❌ PID-файл небезопасен: $PID_FILE" >&2
            return 1
        fi
        saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if [ -n "$saved_pid" ]; then
            if is_valid_process "$saved_pid" "$saved_starttime"; then
                if kill_with_wait "$saved_pid" "$saved_starttime" 10 "бот из PID-файла"; then
                    :
                else
                    echo "❌ Не удалось остановить процесс из PID-файла"
                    return 1
                fi
            else
                echo "⚠️ PID-файл есть, но процесс $saved_pid не соответствует starttime или стал зомби."
            fi
        else
            echo "⚠️ Некорректный PID-файл."
        fi
    fi

    # 3. Поиск и остановка legacy-процессов (запуск без PID-файла)
    local legacy_pids
    legacy_pids=$(find_legacy_pids)
    if [ -n "$legacy_pids" ]; then
        echo "🔍 Найдены legacy-процессы: $legacy_pids"
        for pid in $legacy_pids; do
            # Пропускаем PID, который уже обработан через PID-файл
            if [ -f "$PID_FILE" ] && secure_private_file "$PID_FILE"; then
                local file_pid
                file_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
                if [ "$pid" == "$file_pid" ]; then
                    continue
                fi
            fi

            if ! stop_legacy_process "$pid" "legacy-процесс $pid"; then
                echo "❌ Не удалось остановить legacy-процесс $pid"
                return 1
            fi
        done
    else
        echo "ℹ️ Legacy-процессы не найдены"
    fi

    echo "✅ Все процессы остановлены"
    return 0
}

# --------------------------------------------
# Команда restart
# --------------------------------------------
cmd_restart() {
    echo "🔄 Перезапуск бота ($ENV_NAME)..."
    if ! cmd_stop; then
        echo "❌ Не удалось остановить бота, перезапуск прерван."
        exit 1
    fi
    sleep 1.5
    cmd_start
}

# --------------------------------------------
# Команда status (для CI требуется ровно "БОТ РАБОТАЕТ")
# --------------------------------------------
cmd_status() {
    if [ -f "$PID_FILE" ] || [ -L "$PID_FILE" ]; then
        local saved_pid saved_starttime
        if ! secure_private_file "$PID_FILE"; then
            echo "❌ PID-файл небезопасен: $PID_FILE" >&2
            return 1
        fi
        saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if [ -n "$saved_pid" ] && is_valid_process "$saved_pid" "$saved_starttime"; then
            echo "БОТ РАБОТАЕТ"
            echo "   PID: $saved_pid, ENV: $ENV_NAME, SHM: $SHM_DIR" >&2
            return 0
        else
            echo "БОТ ОСТАНОВЛЕН"
            echo "   PID-файл есть, но процесс мёртв/зомби/переиспользован." >&2
            return 1
        fi
    else
        local legacy_pid
        legacy_pid=$(find_legacy_pids | sed -n '1p')
        if [ -n "$legacy_pid" ]; then
            echo "БОТ ОСТАНОВЛЕН"
            echo "   Найден legacy-процесс (PID $legacy_pid) без PID-файла. Рекомендуется выполнить stop." >&2
            return 1
        else
            echo "БОТ ОСТАНОВЛЕН"
            return 1
        fi
    fi
}

# --------------------------------------------
# Команда backup (вызывается cron'ом)
# --------------------------------------------
cmd_backup() {
    do_backup
}

# --------------------------------------------
# Просмотр логов
# --------------------------------------------
cmd_logs() {
    local follow_flag=""
    if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--follow" ]; }; then
        echo "Использование: $0 logs [--follow]" >&2
        return 2
    fi
    if [ "$#" -eq 1 ]; then
        follow_flag="-f"
    fi

    if [ -f "$LOG_FILE" ] && secure_private_file "$LOG_FILE"; then
        if [ -n "$follow_flag" ]; then
            tail -n 10 -f "$LOG_FILE"
        else
            tail -n 10 "$LOG_FILE"
        fi
    elif [ -e "$LOG_FILE" ] || [ -L "$LOG_FILE" ]; then
        echo "❌ Файл лога небезопасен для чтения: $LOG_FILE" >&2
        return 1
    else
        echo "❌ Лог не найден: $LOG_FILE"
        return 1
    fi
}

cmd_debug_logs() {
    local follow_flag=""
    if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--follow" ]; }; then
        echo "Использование: $0 debug-logs [--follow]" >&2
        return 2
    fi
    if [ "$#" -eq 1 ]; then
        follow_flag="-f"
    fi

    if [ -f "$DEBUG_LOG_FILE" ] && secure_private_file "$DEBUG_LOG_FILE"; then
        if [ -n "$follow_flag" ]; then
            tail -n 15 -f "$DEBUG_LOG_FILE"
        else
            tail -n 15 "$DEBUG_LOG_FILE"
        fi
    elif [ -e "$DEBUG_LOG_FILE" ] || [ -L "$DEBUG_LOG_FILE" ]; then
        echo "❌ Debug-лог небезопасен для чтения: $DEBUG_LOG_FILE" >&2
        return 1
    else
        echo "❌ Лог не найден: $DEBUG_LOG_FILE"
        return 1
    fi
}

cmd_clear_logs() {
    for f in "$LOG_FILE" "$DEBUG_LOG_FILE" "$NOHUP_LOG"; do
        if [ -e "$f" ] || [ -L "$f" ]; then
            if ! secure_log_file_for_backup "$f"; then
                echo "❌ Файл лога небезопасен для очистки: $f" >&2
                return 1
            fi
            : > "$f"
        fi
    done
    echo "🧹 Логи очищены."
    exit 0
}

# --------------------------------------------
# Диспетчер команд
# --------------------------------------------
case "$1" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart
        ;;
    status)
        cmd_status
        ;;
    backup)
        cmd_backup
        ;;
    logs)
        shift
        cmd_logs "$@"
        ;;
    debug-logs)
        shift
        cmd_debug_logs "$@"
        ;;
    clear-logs)
        cmd_clear_logs
        ;;
    *)
        echo "📋 Использование: $0 {start|stop|restart|status|backup|logs [--follow]|debug-logs [--follow]|clear-logs}"
        echo "   ENV определяется автоматически по имени каталога ($ENV_NAME)."
        exit 1
        ;;
esac