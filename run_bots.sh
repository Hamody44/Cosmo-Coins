#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_FALLBACK="${PYTHON:-python3}"
PIDS=()

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

    if [[ ! -x "$interpreter" ]]; then
        printf 'إنشاء بيئة Python مستقلة لـ %s...\n' "$name"
        "$PYTHON_FALLBACK" -m venv "$venv_dir" || {
            printf 'خطأ: تعذر إنشاء البيئة الافتراضية لـ %s.\n' "$name" >&2
            return 1
        }
    fi

    current_hash="$(sha256sum "$requirements" | awk '{print $1}')" || return 1
    if [[ ! -f "$marker" ]] || [[ "$(cat "$marker")" != "$current_hash" ]]; then
        printf 'تثبيت/تحديث متطلبات %s...\n' "$name"
        "$interpreter" -m pip install --disable-pip-version-check -r "$requirements" || {
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

    if [[ ! -f "$directory/main.py" ]]; then
        printf 'خطأ: لم يتم العثور على %s/main.py\n' "$directory" >&2
        return 1
    fi

    prepare_environment "$name" "$directory" "$requirements" || return 1

    printf 'تشغيل %s من %s باستخدام %s\n' "$name" "$directory" "$interpreter"
    (cd -- "$directory" && exec "$interpreter" main.py) &
    PIDS+=("$!")
}

stop_bots() {
    local pid
    trap - INT TERM
    for pid in "${PIDS[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    for pid in "${PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
}

trap stop_bots INT TERM

launch_bot "Cosmo-Coins" "$ROOT_DIR" "$ROOT_DIR/requirements.txt" || exit 1
launch_bot "second_bot" "$ROOT_DIR/second_bot" "$ROOT_DIR/second_bot/requirements.txt" || { stop_bots; exit 1; }

# إذا توقف أحد البوتين، أوقف الآخر بدل تركه يعمل وحده دون مراقبة.
if wait -n "${PIDS[@]}"; then
    status=0
else
    status=$?
fi
printf 'توقف أحد البوتين (رمز الخروج: %s)، يجري إيقاف الآخر.\n' "$status" >&2
stop_bots
exit "$status"
