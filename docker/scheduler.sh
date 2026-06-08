#!/usr/bin/env sh
# Long-running scheduler: re-run the configured search every LJP_SCHEDULE_INTERVAL
# seconds (default 3600). A lightweight in-process loop — no extra cron binary.
#
# After each successful run it stamps a heartbeat file that the container
# healthcheck (docker/healthcheck.sh) reads to decide liveness.
set -eu

INTERVAL="${LJP_SCHEDULE_INTERVAL:-3600}"
HEARTBEAT="${LJP_HEARTBEAT_FILE:-/tmp/ljp_heartbeat}"

echo "[scheduler] starting; interval=${INTERVAL}s heartbeat=${HEARTBEAT}"

# Seed the heartbeat so the container is healthy during the first run.
date +%s > "$HEARTBEAT"

while true; do
    echo "[scheduler] $(date -u +%Y-%m-%dT%H:%M:%SZ) run starting"
    if /app/docker/entrypoint.sh; then
        date +%s > "$HEARTBEAT"
        echo "[scheduler] run ok"
    else
        echo "[scheduler] run FAILED (exit $?)" >&2
    fi
    echo "[scheduler] sleeping ${INTERVAL}s"
    sleep "$INTERVAL"
done
