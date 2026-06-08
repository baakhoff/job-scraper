#!/usr/bin/env sh
# Entrypoint for linkedin-job-parser.
#
#   * With arguments: exec them verbatim (e.g. `python main.py list --limit 20`,
#     or the scheduler loop passed as the compose `command`).
#   * With no arguments: run a single search assembled from LJP_SEARCH_* env vars.
set -eu

run_search() {
    # Build argv incrementally so optional flags are only added when set.
    set -- python main.py search \
        --keywords "${LJP_SEARCH_KEYWORDS:-python}" \
        --max-results "${LJP_SEARCH_MAX_RESULTS:-25}"

    if [ -n "${LJP_SEARCH_LOCATION:-}" ]; then
        set -- "$@" --location "$LJP_SEARCH_LOCATION"
    fi
    if [ -n "${LJP_SEARCH_GEO_ID:-}" ]; then
        set -- "$@" --geo-id "$LJP_SEARCH_GEO_ID"
    fi
    if [ -n "${LJP_SEARCH_WORKPLACE_TYPE:-}" ]; then
        set -- "$@" --workplace-type "$LJP_SEARCH_WORKPLACE_TYPE"
    fi
    if [ "${LJP_SEARCH_DETAILS:-0}" = "1" ]; then
        set -- "$@" --details
    fi

    echo "[entrypoint] $*"
    exec "$@"
}

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

run_search
