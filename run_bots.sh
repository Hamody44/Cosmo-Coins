#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_FALLBACK="${PYTHON:-python3}"
PIDS=()

launch_bot() {
    local name="$1"
    local directory="$2"
    local interpreter="$PYTHON_FALLBACK"

    if [[ -x "$directory/.venv/bin/python" ]]; then
        interpreter="$directory/.venv/bin/python"
    fi

    if [[ ! -f "$directory/main.py" ]]; then
        printf 'خطأ: لم يتم العثور على %s/main.py\n' "$directory" >&2
        return 1
    fi

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

launch_bot "Cosmo-Coins" "$ROOT_DIR" || exit 1
launch_bot "second_bot" "$ROOT_DIR/second_bot" || { stop_bots; exit 1; }

# إذا توقف أحد البوتين، أوقف الآخر بدل تركه يعمل وحده دون مراقبة.
if wait -n "${PIDS[@]}"; then
    status=0
else
    status=$?
fi
printf 'توقف أحد البوتين (رمز الخروج: %s)، يجري إيقاف الآخر.\n' "$status" >&2
stop_bots
exit "$status"
