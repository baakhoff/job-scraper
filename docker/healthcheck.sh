#!/usr/bin/env sh
# Container healthcheck for the scheduler service.
#
# Healthy when the scheduler has stamped its heartbeat within the last two
# intervals (plus a small grace), i.e. the loop is alive and making runs.
set -eu

HEARTBEAT="${LJP_HEARTBEAT_FILE:-/tmp/ljp_heartbeat}"
INTERVAL="${LJP_SCHEDULE_INTERVAL:-3600}"

[ -f "$HEARTBEAT" ] || exit 1

now=$(date +%s)
last=$(cat "$HEARTBEAT")
age=$(( now - last ))
max=$(( INTERVAL * 2 + 120 ))

[ "$age" -lt "$max" ]
