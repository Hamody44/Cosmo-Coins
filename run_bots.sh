#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_FALLBACK="${PYTHON:-python3}"
STATE_DIR="$ROOT_DIR/second_bot/.runtime/process_control"
SUPERVISOR_PID_FILE="$STATE_DIR/supervisor.pid"
REQUEST_FILE="$STATE_DIR/request"

mkdir -p "$STATE_DIR"

pid_file() {
    printf '%s/%s.pid' "$STATE_DIR" "$1"
}

read_pid() {
    local file
    file="$(pid_file "$1")"
    [[ -f "$file" ]] || return 1
    cat "$file"
}

pid_is_alive() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

bot_is_running() {
    local pid
    pid="$(read_pid "$1" 2>/dev/null || true)"
    pid_is_alive "$pid"
}

status_bot() {
    local name="$1"
    local pid
    pid="$(read_pid "$name" 2>/dev/null || true)"
    if pid_is_alive "$pid"; then
        printf '%s: running (PID %s)\n' "$name" "$pid"
    else
        printf '%s: stopped\n' "$name"
    fi
}

supervisor_is_running() {
    local pid=""
    [[ -f "$SUPERVISOR_PID_FILE" ]] && pid="$(cat "$SUPERVISOR_PID_FILE" 2>/dev/null || true)"
    pid_is_alive "$pid"
}

validate_bot_name() {
    [[ "$1" == "primary" || "$1" == "second_bot" ]]
}

# This mode is called by Discord commands. It only queues a request; the
# long-running supervisor performs the process operation safely.
if [[ "${1:-}" == "control" ]]; then
    action="${2:-}"
    target="${3:-}"
    if [[ "$action" != "status" && "$action" != "start" && "$action" != "restart" && "$action" != "shutdown" ]]; then
        printf 'usage: %s control {status|start|restart|shutdown} {primary|second_bot}\n' "$0" >&2
        exit 2
    fi
    if ! validate_bot_name "$target"; then
        printf 'unknown bot: %s\n' "$target" >&2
        exit 2
    fi
    if [[ "$action" == "status" ]]; then
        status_bot "$target"
        exit 0
    fi
    if ! supervisor_is_running; then
        printf 'supervisor: stopped; cannot queue %s for %s\n' "$action" "$target" >&2
        exit 1
    fi
    request_tmp="$REQUEST_FILE.$$"
    printf '%s %s\n' "$action" "$target" > "$request_tmp"
    mv -f "$request_tmp" "$REQUEST_FILE"
    printf 'queued: %s %s\n' "$action" "$target"
    exit 0
fi

if supervisor_is_running; then
    printf 'خطأ: run_bots.sh يعمل بالفعل.\n' >&2
    exit 1
fi
printf '%s\n' "$$" > "$SUPERVISOR_PID_FILE"

prepare_environment() {
    local name="$1"
    local directory="$2"
    local requirements="$3"
    local venv_dir="$directory/.venv"
    local interpreter="$venv_dir/bin/python"
    local marker="$venv_dir/.requirements.sha256"
    local current_hash

    if [[ ! -f "$requirements" ]]; then
        printf 'خطأ: لم يتم العثور على ملف المتطلبات: %s\n' "$requirements" >&2
        return 1
    fi

    current_hash="$(sha256sum "$requirements" | awk '{print $1}')" || return 1

    if [[ -x "$interpreter" && -f "$marker" && "$(cat "$marker")" != "$current_hash" ]]; then
        printf 'تغيرت المتطلبات؛ إعادة إنشاء البيئة النظيفة لـ %s...\n' "$name"
        rm -rf "$venv_dir"
    fi

    if [[ ! -x "$interpreter" ]]; then
        printf 'إنشاء بيئة Python مستقلة لـ %s...\n' "$name"
        "$PYTHON_FALLBACK" -m venv "$venv_dir" || {
            printf 'خطأ: تعذر إنشاء البيئة الافتراضية لـ %s.\n' "$name" >&2
            return 1
        }
    fi

    if [[ ! -f "$marker" ]] || [[ "$(cat "$marker")" != "$current_hash" ]]; then
        printf 'تثبيت/تحديث متطلبات %s...\n' "$name"
        "$interpreter" -m pip install --disable-pip-version-check --no-cache-dir -r "$requirements" || {
            printf 'خطأ: تعذر تثبيت متطلبات %s.\n' "$name" >&2
            return 1
        }
        printf '%s\n' "$current_hash" > "$marker"
    fi
}

launch_bot() {
    local name="$1"
    local directory="$2"
    local requirements="$3"
    local interpreter="$directory/.venv/bin/python"

    if bot_is_running "$name"; then
        printf '%s يعمل بالفعل.\n' "$name"
        return 0
    fi
    if [[ ! -f "$directory/main.py" ]]; then
        printf 'خطأ: لم يتم العثور على %s/main.py\n' "$directory" >&2
        return 1
    fi

    prepare_environment "$name" "$directory" "$requirements" || return 1

    printf 'تشغيل %s من %s باستخدام %s\n' "$name" "$directory" "$interpreter"
    (
        cd -- "$directory" || exit 1
        exec "$interpreter" main.py
    ) &
    printf '%s\n' "$!" > "$(pid_file "$name")"
}

stop_bot() {
    local name="$1"
    local pid
    pid="$(read_pid "$name" 2>/dev/null || true)"
    if ! pid_is_alive "$pid"; then
        rm -f "$(pid_file "$name")"
        return 0
    fi

    printf 'إيقاف %s (PID %s)\n' "$name" "$pid"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..40}; do
        if ! pid_is_alive "$pid"; then
            break
        fi
        sleep 0.25
    done
    if pid_is_alive "$pid"; then
        printf 'لم يتوقف %s بشكل نظيف؛ سيتم إنهاؤه بالقوة.\n' "$name" >&2
        kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
    rm -f "$(pid_file "$name")"
}

start_target() {
    case "$1" in
        primary)
            launch_bot "primary" "$ROOT_DIR" "$ROOT_DIR/cosmo_requirements.lock"
            ;;
        second_bot)
            launch_bot "second_bot" "$ROOT_DIR/second_bot" "$ROOT_DIR/second_bot/cosmo_second_requirements.lock"
            ;;
    esac
}

handle_request() {
    [[ -f "$REQUEST_FILE" ]] || return 0
    local request_file="$REQUEST_FILE.processing.$$"
    mv -f "$REQUEST_FILE" "$request_file" 2>/dev/null || return 0

    local action target
    read -r action target < "$request_file"
    rm -f "$request_file"
    case "$action" in
        start)
            start_target "$target"
            ;;
        restart)
            stop_bot "$target"
            start_target "$target"
            ;;
        shutdown)
            stop_bot "$target"
            ;;
    esac
}

cleanup() {
    trap - INT TERM EXIT
    stop_bot "primary"
    stop_bot "second_bot"
    rm -f "$SUPERVISOR_PID_FILE" "$REQUEST_FILE"
}

trap cleanup INT TERM EXIT

launch_bot "primary" "$ROOT_DIR" "$ROOT_DIR/cosmo_requirements.lock" || exit 1
launch_bot "second_bot" "$ROOT_DIR/second_bot" "$ROOT_DIR/second_bot/cosmo_second_requirements.lock" || {
    stop_bot "primary"
    exit 1
}

printf 'تم تشغيل البوتين؛ التحكم المنفصل متاح عبر أوامر Discord.\n'
while :; do
    handle_request
    sleep 0.5
done
