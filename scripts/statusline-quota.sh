#!/usr/bin/env bash
# Claude Code statusline script for gcli2api quota monitoring.
# Displays upstream Antigravity quota percentages with color coding.
#
# Configuration (env vars):
#   GCLI2API_URL  - gcli2api server URL (default: http://127.0.0.1:7861)
#   GCLI2API_KEY  - API key for authentication
#
# Usage in ~/.claude/settings.json:
#   {
#     "statusLine": {
#       "type": "command",
#       "command": "GCLI2API_URL=http://127.0.0.1:7861 GCLI2API_KEY=yourkey /path/to/statusline-quota.sh"
#     }
#   }

set -euo pipefail

GCLI2API_URL="${GCLI2API_URL:-http://127.0.0.1:7861}"
GCLI2API_KEY="${GCLI2API_KEY:-}"
CACHE_FILE="/tmp/gcli2api-quota-cache"
CACHE_TTL=30  # seconds

# Read stdin (Claude Code passes JSON with model info and context %)
INPUT=""
if [ ! -t 0 ]; then
    INPUT=$(cat)
fi

# Extract context percentage from Claude Code's JSON
CTX_PCT=""
if [ -n "$INPUT" ]; then
    CTX_PCT=$(echo "$INPUT" | jq -r '.context_window.used_percentage // empty' 2>/dev/null || true)
fi

# Check if we have a valid API key
if [ -z "$GCLI2API_KEY" ]; then
    if [ -n "$CTX_PCT" ]; then
        echo "ctx:${CTX_PCT}% | no API key"
    else
        echo "no API key"
    fi
    exit 0
fi

# Check cache freshness
QUOTA_JSON=""
if [ -f "$CACHE_FILE" ]; then
    CACHE_AGE=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE" 2>/dev/null || echo 0) ))
    if [ "$CACHE_AGE" -lt "$CACHE_TTL" ]; then
        QUOTA_JSON=$(cat "$CACHE_FILE" 2>/dev/null || true)
    fi
fi

# Fetch fresh data if cache is stale
if [ -z "$QUOTA_JSON" ]; then
    QUOTA_JSON=$(curl -s --max-time 5 \
        -H "Authorization: Bearer $GCLI2API_KEY" \
        "$GCLI2API_URL/api/quota" 2>/dev/null || true)

    if [ -n "$QUOTA_JSON" ]; then
        echo "$QUOTA_JSON" > "$CACHE_FILE" 2>/dev/null || true
    fi
fi

# If we still have no data, show minimal output
if [ -z "$QUOTA_JSON" ] || ! echo "$QUOTA_JSON" | jq -e '.success' >/dev/null 2>&1; then
    if [ -n "$CTX_PCT" ]; then
        echo "ctx:${CTX_PCT}% | quota:?"
    else
        echo "quota:?"
    fi
    exit 0
fi

# ANSI color helpers
color_for_pct() {
    local pct=$1
    if [ "$pct" -gt 50 ]; then
        echo "\033[32m"  # green
    elif [ "$pct" -gt 20 ]; then
        echo "\033[33m"  # yellow
    else
        echo "\033[31m"  # red
    fi
}
RESET="\033[0m"

# Extract Opus quota and credential counts
OPUS_PCT=$(echo "$QUOTA_JSON" | jq -r '
    .models
    | to_entries[]
    | select(.key | test("opus"))
    | .value.remaining_pct' 2>/dev/null | sort -rn | head -1)

OPUS_AVAIL=$(echo "$QUOTA_JSON" | jq -r '
    .models
    | to_entries[]
    | select(.key | test("opus"))
    | .value.credentials_available' 2>/dev/null | sort -rn | head -1)

OPUS_TOTAL=$(echo "$QUOTA_JSON" | jq -r '
    .models
    | to_entries[]
    | select(.key | test("opus"))
    | .value.credentials_total' 2>/dev/null | sort -rn | head -1)

# Build output
PARTS=""
if [ -n "$CTX_PCT" ]; then
    PARTS="ctx:${CTX_PCT}%"
fi

QUOTA_PARTS=""
if [ -n "$OPUS_PCT" ] && [ "$OPUS_PCT" != "null" ]; then
    C=$(color_for_pct "$OPUS_PCT")
    QUOTA_PARTS="${C}Op:${OPUS_PCT}${RESET} ${OPUS_AVAIL:-0}/${OPUS_TOTAL:-0}"
fi

# Combine: ctx:42% Op:100 4/6
if [ -n "$PARTS" ] && [ -n "$QUOTA_PARTS" ]; then
    echo -e "${PARTS} ${QUOTA_PARTS}"
elif [ -n "$QUOTA_PARTS" ]; then
    echo -e "${QUOTA_PARTS}"
elif [ -n "$PARTS" ]; then
    echo -e "${PARTS}"
fi
