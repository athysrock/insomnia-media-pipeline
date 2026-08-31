#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
    printf 'usage: %s STORY CONFIG RUN_DIR\n' "$0" >&2
    exit 2
fi

STORY=$1
CONFIG=$2
RUN_DIR=$3
CONDUCTOR_URL=${CONDUCTOR_URL:-http://localhost:8080}
PYTHON=${PYTHON:-python3}
SYSTEM_PYTHON=${SYSTEM_PYTHON:-python3}
TTS_PYTHON=${TTS_PYTHON:-python3}
MUSICGEN_PYTHON=${MUSICGEN_PYTHON:-python3}
POLL_INTERVAL=${POLL_INTERVAL:-2}
GENERAL_WORKER_PID=
export RUN_DIR

stop_general_worker() {
    if [ -n "$GENERAL_WORKER_PID" ]; then
        kill "$GENERAL_WORKER_PID" 2>/dev/null || true
        wait "$GENERAL_WORKER_PID" 2>/dev/null || true
        GENERAL_WORKER_PID=
    fi
}

start_general_worker() {
    "$PYTHON" -m conductor.worker --url "$CONDUCTOR_URL" \
        --exclude-task insomnia_media_pipeline_tts \
        --exclude-task insomnia_media_pipeline_music &
    GENERAL_WORKER_PID=$!
}

require_general_worker() {
    if ! kill -0 "$GENERAL_WORKER_PID" 2>/dev/null; then
        printf 'general worker exited while workflow was active\n' >&2
        exit 1
    fi
}

cleanup() {
    stop_general_worker
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

start_general_worker

launch=$(
    "$PYTHON" -m conductor.client launch --url "$CONDUCTOR_URL" \
        --story "$STORY" --config "$CONFIG" --run-dir "$RUN_DIR"
)
WORKFLOW_ID=$(printf '%s' "$launch" | "$SYSTEM_PYTHON" -c \
    'import json,sys; print(json.load(sys.stdin)["workflow_id"])')

while :; do
    state=$("$PYTHON" -m conductor.client monitor --url "$CONDUCTOR_URL" --workflow-id "$WORKFLOW_ID")
    require_general_worker
    if printf '%s' "$state" | "$SYSTEM_PYTHON" -c \
        'import json,sys; d=json.load(sys.stdin); raise SystemExit(not any(t.get("taskDefName") == "insomnia_media_pipeline_tts" and t.get("status") == "SCHEDULED" for t in d.get("tasks", [])))'
    then
        break
    fi
    status=$(printf '%s' "$state" | "$SYSTEM_PYTHON" -c \
        'import json,sys; print(json.load(sys.stdin).get("status", "UNKNOWN"))')
    case "$status" in
        FAILED|TIMED_OUT|TERMINATED|PAUSED_WITH_ERRORS)
            printf 'workflow ended with status %s\n' "$status" >&2
            exit 1
            ;;
    esac
    sleep "$POLL_INTERVAL"
done

stop_general_worker
"$TTS_PYTHON" -m conductor.worker --url "$CONDUCTOR_URL" \
    --task insomnia_media_pipeline_tts --once
test -f "$RUN_DIR/staging/receipts/tts.json"

start_general_worker
while :; do
    state=$("$PYTHON" -m conductor.client monitor --url "$CONDUCTOR_URL" --workflow-id "$WORKFLOW_ID")
    require_general_worker
    if printf '%s' "$state" | "$SYSTEM_PYTHON" -c \
        'import json,sys; d=json.load(sys.stdin); raise SystemExit(not any(t.get("taskDefName") == "insomnia_media_pipeline_music" and t.get("status") == "SCHEDULED" for t in d.get("tasks", [])))'
    then
        break
    fi
    status=$(printf '%s' "$state" | "$SYSTEM_PYTHON" -c \
        'import json,sys; print(json.load(sys.stdin).get("status", "UNKNOWN"))')
    case "$status" in
        FAILED|TIMED_OUT|TERMINATED|PAUSED_WITH_ERRORS)
            printf 'workflow ended with status %s\n' "$status" >&2
            exit 1
            ;;
    esac
    sleep "$POLL_INTERVAL"
done
stop_general_worker

"$MUSICGEN_PYTHON" -m conductor.worker --url "$CONDUCTOR_URL" \
    --task insomnia_media_pipeline_music --once
test -f "$RUN_DIR/staging/receipts/music.json"
start_general_worker

while :; do
    state=$("$PYTHON" -m conductor.client monitor --url "$CONDUCTOR_URL" --workflow-id "$WORKFLOW_ID")
    require_general_worker
    if printf '%s' "$state" | "$SYSTEM_PYTHON" -c \
        'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("status") == "COMPLETED" else 1)'
    then
        break
    fi
    status=$(printf '%s' "$state" | "$SYSTEM_PYTHON" -c \
        'import json,sys; print(json.load(sys.stdin).get("status", "UNKNOWN"))')
    case "$status" in
        FAILED|TIMED_OUT|TERMINATED|PAUSED_WITH_ERRORS)
            printf 'workflow ended with status %s\n' "$status" >&2
            exit 1
            ;;
    esac
    sleep "$POLL_INTERVAL"
done

printf 'workflow %s completed\n' "$WORKFLOW_ID"
