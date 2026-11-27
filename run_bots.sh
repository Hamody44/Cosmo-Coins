#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_FALLBACK="${PYTHON:-python3}"
STATE_DIR="$ROOT_DIR/second_bot/.runtime/process_control"
SUPERVISOR_PID_FILE="$STATE_DIR/supervisor.pid"
REQUESTS_DIR="$ROOT_DIR/requests"
REQUEST_INBOX="$REQUESTS_DIR/inbox"
REQUEST_PROCESSING="$REQUESTS_DIR/processing"
SECOND_BOT_ACTIVITY_FILE="$STATE_DIR/second_bot.activity"
SECOND_BOT_IDLE_SECONDS="${SECOND_BOT_IDLE_SECONDS:-90}"

if ! [[ "$SECOND_BOT_IDLE_SECONDS" =~ ^[0-9]+$ ]] || (( SECOND_BOT_IDLE_SECONDS < 1 )); then
    printf 'خطأ: SECOND_BOT_IDLE_SECONDS يجب أن يكون عدداً صحيحاً أكبر من صفر.\n' >&2
    exit 2
fi

mkdir -p "$STATE_DIR" "$REQUEST_INBOX" "$REQUEST_PROCESSING" "$REQUESTS_DIR/done" "$REQUESTS_DIR/failed"

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

mark_second_bot_activity() {
    touch "$SECOND_BOT_ACTIVITY_FILE"
}

second_bot_idle() {
    local last_activity now
    [[ -f "$SECOND_BOT_ACTIVITY_FILE" ]] || return 0
    last_activity="$(stat -c '%Y' "$SECOND_BOT_ACTIVITY_FILE" 2>/dev/null || printf '0')"
    now="$(date +%s)"
    [[ "$last_activity" =~ ^[0-9]+$ ]] || return 0
    (( now - last_activity >= SECOND_BOT_IDLE_SECONDS ))
}

stop_idle_second_bot() {
    if bot_is_running "second_bot" && second_bot_idle; then
        printf 'إيقاف second_bot بعد %s ثانية من الخمول.\n' "$SECOND_BOT_IDLE_SECONDS"
        stop_bot "second_bot"
        rm -f "$SECOND_BOT_ACTIVITY_FILE"
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
    requester_id="${4:-${PRIMARY_OWNER_ID:-}}"
    if [[ -z "$requester_id" ]]; then
        requester_id="$(PYTHONPATH="$ROOT_DIR" "$PYTHON_FALLBACK" -c 'from brain_bot.__main__ import owner_id; print(owner_id() or "")')"
    fi
    if [[ "$action" != "status" && "$action" != "start" && "$action" != "restart" && "$action" != "shutdown" ]]; then
        printf 'usage: %s control {status|start|restart|shutdown} {primary|second_bot} [requester_id]\n' "$0" >&2
        exit 2
    fi
    if ! validate_bot_name "$target"; then
        printf 'unknown bot: %s\n' "$target" >&2
        exit 2
    fi
    if [[ "$action" == "status" ]]; then
        status_bot "$target"
        printf '%s\n' "brain: $(PYTHONPATH="$ROOT_DIR" "$PYTHON_FALLBACK" -m brain_bot status 2>/dev/null || printf 'unavailable')"
        exit 0
    fi
    if ! [[ "$requester_id" =~ ^[0-9]+$ ]]; then
        printf 'rejected: requester_id must be numeric\n' >&2
        exit 1
    fi
    if ! queued_output=$(cd "$ROOT_DIR" && PYTHONPATH="$ROOT_DIR" "$PYTHON_FALLBACK" -m brain_bot request "$action" "$target" "$requester_id" --source "run_bots.sh" 2>&1); then
        printf '%s\n' "$queued_output" >&2
        exit 1
    fi
    if [[ "$target" == "second_bot" ]]; then
        mark_second_bot_activity
    fi
    printf '%s\n' "$queued_output"
    exit 0
fi

# The PID check is useful for diagnostics but is not atomic by itself.
# Hold an OS-level lock for the supervisor lifetime so simultaneous panel
# starts cannot each launch another copy of primary/second_bot.
exec 9>"$STATE_DIR/supervisor.lock"
if ! flock -n 9; then
    printf 'خطأ: يوجد مشرف run_bots.sh آخر يعمل بالفعل.\n' >&2
    exit 1
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
        exec 9>&-
        cd -- "$directory" || exit 1
        exec "$interpreter" main.py
    ) >>"$STATE_DIR/$name.log" 2>&1 &
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
            mark_second_bot_activity
            launch_bot "second_bot" "$ROOT_DIR/second_bot" "$ROOT_DIR/second_bot/cosmo_second_requirements.lock"
            ;;
    esac
}

handle_request() {
    local request_file request_id action target requester_id expected_owner
    request_file="$(find "$REQUEST_INBOX" -maxdepth 1 -type f -name '*.json' -print -quit 2>/dev/null || true)"
    [[ -n "$request_file" ]] || return 0
    request_id="$(basename "$request_file" .json)"
    local processing_file="$REQUEST_PROCESSING/$request_id.json"
    mv -f "$request_file" "$processing_file" 2>/dev/null || return 0
    if ! IFS=$'\t' read -r action target requester_id < <(python3 - "$processing_file" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    print(f"{payload.get('action','')}\t{payload.get('target','')}\t{payload.get('requester_id','')}")
except Exception:
    print("\t\t")
PY
    ); then
        action=""
        target=""
        requester_id=""
    fi
    local result=0
    expected_owner="$(PYTHONPATH="$ROOT_DIR" "$PYTHON_FALLBACK" -c 'from brain_bot.__main__ import owner_id; print(owner_id() or "")')"
    if [[ ! "$requester_id" =~ ^[0-9]+$ ]] || [[ "$requester_id" != "$expected_owner" ]] || ! validate_bot_name "$target"; then
        result=1
    fi
    case "$action" in
        start) if (( result == 0 )); then start_target "$target" || result=$?; fi ;;
        restart) if (( result == 0 )); then stop_bot "$target"; start_target "$target" || result=$?; fi ;;
        shutdown)
            if (( result == 0 )); then stop_bot "$target"; fi
            [[ "$target" == "second_bot" ]] && rm -f "$SECOND_BOT_ACTIVITY_FILE"
            ;;
        *) result=1 ;;
    esac
    if (( result == 0 )); then
        PYTHONPATH="$ROOT_DIR" "$PYTHON_FALLBACK" - <<PY
from brain_bot.protocol import finish
finish("$request_id", status="done")
PY
    else
        PYTHONPATH="$ROOT_DIR" "$PYTHON_FALLBACK" - <<PY
from brain_bot.protocol import finish
finish("$request_id", status="failed", error="process operation failed")
PY
    fi
}

cleanup() {
    trap - INT TERM EXIT
    stop_bot "brain_bot"
    stop_bot "primary"
    stop_bot "second_bot"
    rm -f "$SUPERVISOR_PID_FILE" "$SECOND_BOT_ACTIVITY_FILE"
}

trap cleanup INT TERM EXIT

# The original process manager starts all three independent clients at boot.
launch_bot "primary" "$ROOT_DIR" "$ROOT_DIR/cosmo_requirements.lock" || exit 1
launch_bot "second_bot" "$ROOT_DIR/second_bot" "$ROOT_DIR/second_bot/cosmo_second_requirements.lock" || exit 1
launch_bot "brain_bot" "$ROOT_DIR/brain_bot" "$ROOT_DIR/brain_bot/requirements.txt" || exit 1

printf 'تم تشغيل العمليات الثلاث: primary وsecond_bot وbrain_bot.\n'
while :; do
    handle_request
    PYTHONPATH="$ROOT_DIR" "$PYTHON_FALLBACK" -m brain_bot heartbeat >/dev/null 2>&1 || true
    sleep 0.5
done
