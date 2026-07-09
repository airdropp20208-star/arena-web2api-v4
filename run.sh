#!/bin/bash
# Auto-restart wrapper cho arena-web2api.
# - Pre-start checks (port, deps, .env)
# - Restart server nếu crash, exponential backoff
# - Log ra stdout + /tmp/arena-web2api.log

set -u
cd "$(dirname "$0")"

LOG_FILE="${LOG_FILE:-/tmp/arena-web2api.log}"
MAX_BACKOFF=60
backoff=5

echo "🚀 Starting arena-web2api auto-restart wrapper"
echo "   Log: $LOG_FILE"
echo "   Stop: kill $! or Ctrl-C"

# Pre-start checks — fix #5 (port conflict) + #8 (curl/deps)
echo "Running pre-start checks..."
python3 precheck.py
precheck_status=$?
if [ $precheck_status -ne 0 ]; then
    echo "❌ Pre-start checks failed. Fix errors above then retry."
    exit 1
fi
echo ""

while true; do
    echo ""
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ▶ Starting server (backoff=${backoff}s)..."
    python3 main.py 2>&1 | tee -a "$LOG_FILE"
    exit_code=$?

    if [ $exit_code -eq 0 ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] ✋ Server exited cleanly (exit 0)"
        break
    fi

    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ❌ Server crashed (exit=$exit_code), restarting in ${backoff}s..."
    sleep $backoff

    # Exponential backoff capped at MAX_BACKOFF
    backoff=$((backoff * 2))
    if [ $backoff -gt $MAX_BACKOFF ]; then
        backoff=$MAX_BACKOFF
    fi
done
