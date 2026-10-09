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
LEGACY_BOT_SCRIPT="$BOT_SCRIPT"

# --------------------------------------------
# Определяем Python (venv приоритетно)
# --------------------------------------------
if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
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

PID_FILE="$SHM_DIR/reminder_bot_bot.pid"
LOG_FILE="$SHM_DIR/reminder_bot_interaction.log"
DEBUG_LOG_FILE="$SHM_DIR/reminder_bot_debug.log"
NOHUP_LOG="$SHM_DIR/sys_nohup.log"

# Персистентные бэкапы логов на SD-карте
BACKUP_DIR="$PROJECT_DIR/logs_backup"

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
    mkdir -p "$BACKUP_DIR"
    if [ -s "$LOG_FILE" ]; then
        local backup_file="$BACKUP_DIR/reminder_bot_interaction_$(date +%F).log"
        cp -a "$LOG_FILE" "$backup_file"
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
    local latest
    latest=$(ls -1t "$BACKUP_DIR"/reminder_bot_interaction_*.log 2>/dev/null | head -n 1)
    if [ -n "$latest" ]; then
        cp -a "$latest" "$LOG_FILE"
        echo "♻️ Восстановлен interaction.log из $latest"
    fi
}

# --------------------------------------------
# Команда start
# --------------------------------------------
cmd_start() {
    echo "🚀 Запуск бота ($ENV_NAME)..."

    mkdir -p "$SHM_DIR"
    mkdir -p "$BACKUP_DIR"
    chmod 775 "$SHM_BASE" 2>/dev/null || true
    chmod 775 "$SHM_DIR" 2>/dev/null || true

    # Проверка активного PID-файла
    if [ -f "$PID_FILE" ]; then
        local saved_pid saved_starttime
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
    restore_latest_log

    if [ ! -f "$BOT_SCRIPT" ]; then
        echo "❌ Не найден основной файл бота: $BOT_SCRIPT"
        exit 1
    fi

    cd "$PROJECT_DIR" || exit 1

    PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        RUNNER_TRACKING_ID="${RUNNER_TRACKING_ID:-0}" \
        nohup "$PYTHON_EXEC" -u "$BOT_SCRIPT" \
        --shm-dir "$SHM_DIR" \
        --log-dir "$SHM_DIR" \
        > "$NOHUP_LOG" 2>&1 &
    local new_pid=$!

    sleep 0.5
    local new_starttime
    new_starttime=$(get_process_starttime "$new_pid")
    if [ -z "$new_starttime" ]; then
        echo "❌ Не удалось получить время старта процесса $new_pid"
        rm -f "$PID_FILE"
        exit 1
    fi

    echo "$new_pid:$new_starttime" > "$PID_FILE"

    sleep 1
    if is_valid_process "$new_pid" "$new_starttime"; then
        echo "✅ Бот успешно запущен (PID $new_pid) [$ENV_NAME]."
        echo "📄 interaction.log: $LOG_FILE"
        echo "📄 debug.log:       $DEBUG_LOG_FILE"
        exit 0
    else
        echo "❌ Ошибка старта! Проверьте $NOHUP_LOG"
        rm -f "$PID_FILE"
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
    if [ -f "$PID_FILE" ]; then
        local saved_pid saved_starttime
        saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if [ -n "$saved_pid" ]; then
            if is_valid_process "$saved_pid" "$saved_starttime"; then
                if kill_with_wait "$saved_pid" "$saved_starttime" 10 "бот из PID-файла"; then
                    rm -f "$PID_FILE"
                else
                    echo "❌ Не удалось остановить процесс из PID-файла"
                    return 1
                fi
            else
                echo "⚠️ PID-файл есть, но процесс $saved_pid не соответствует starttime или стал зомби. Удаляем файл."
                rm -f "$PID_FILE"
            fi
        else
            echo "⚠️ Некорректный PID-файл, удаляем."
            rm -f "$PID_FILE"
        fi
    fi

    # 3. Поиск и остановка legacy-процессов (запуск без PID-файла)
    local legacy_pids
    legacy_pids=$(pgrep -f "python.*$LEGACY_BOT_SCRIPT" 2>/dev/null)
    if [ -n "$legacy_pids" ]; then
        echo "🔍 Найдены legacy-процессы: $legacy_pids"
        for pid in $legacy_pids; do
            # Пропускаем PID, который уже обработан через PID-файл
            if [ -f "$PID_FILE" ]; then
                local file_pid
                file_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
                if [ "$pid" == "$file_pid" ]; then
                    continue
                fi
            fi

            local cmdline
            cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
            if [[ "$cmdline" == *"$LEGACY_BOT_SCRIPT"* ]]; then
                if ! stop_legacy_process "$pid" "legacy-процесс $pid"; then
                    echo "❌ Не удалось остановить legacy-процесс $pid"
                    return 1
                fi
            else
                echo "ℹ️ Процесс $pid не относится к этому боту (пропускаем)."
            fi
        done
    else
        echo "ℹ️ Legacy-процессы не найдены"
    fi

    rm -f "$PID_FILE"
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
    if [ -f "$PID_FILE" ]; then
        local saved_pid saved_starttime
        saved_pid=$(cut -d: -f1 "$PID_FILE" 2>/dev/null)
        saved_starttime=$(cut -d: -f2 "$PID_FILE" 2>/dev/null)
        if [ -n "$saved_pid" ] && is_valid_process "$saved_pid" "$saved_starttime"; then
            echo "БОТ РАБОТАЕТ"
            echo "   PID: $saved_pid, ENV: $ENV_NAME, SHM: $SHM_DIR" >&2
            return 0
        else
            echo "БОТ ОСТАНОВЛЕН"
            echo "   PID-файл есть, но процесс мёртв/зомби/переиспользован. Удаляем." >&2
            rm -f "$PID_FILE"
            return 1
        fi
    else
        local legacy_pid
        legacy_pid=$(pgrep -f "python.*$LEGACY_BOT_SCRIPT" 2>/dev/null | head -n 1)
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
    if [ -f "$LOG_FILE" ]; then
        tail -n 10 -f "$LOG_FILE"
    else
        echo "❌ Лог не найден: $LOG_FILE"
        exit 1
    fi
}

cmd_debug_logs() {
    if [ -f "$DEBUG_LOG_FILE" ]; then
        tail -n 15 -f "$DEBUG_LOG_FILE"
    else
        echo "❌ Лог не найден: $DEBUG_LOG_FILE"
        exit 1
    fi
}

cmd_clear_logs() {
    for f in "$LOG_FILE" "$DEBUG_LOG_FILE" "$NOHUP_LOG"; do
        [ -f "$f" ] && true > "$f"
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
        cmd_logs
        ;;
    debug-logs)
        cmd_debug_logs
        ;;
    clear-logs)
        cmd_clear_logs
        ;;
    *)
        echo "📋 Использование: $0 {start|stop|restart|status|backup|logs|debug-logs|clear-logs}"
        echo "   ENV определяется автоматически по имени каталога ($ENV_NAME)."
        exit 1
        ;;
esac