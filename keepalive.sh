#!/data/data/com.termux/files/usr/bin/bash
# keepalive.sh — chạy trong Termux, giám sát:
#   1. Server arena-web2api (port 8000) — restart nếu chết
#   2. Kiwi Browser process — relaunch nếu bị Android kill
#   3. Tab arena.ai còn sống (qua /admin/broker) — báo nếu disconnect
#
# Chạy ngầm hoàn toàn: phone có thể tắt màn hình, chơi game, v.v.
# Setup: nohup bash keepalive.sh > /tmp/keepalive.log 2>&1 &
# Stop: pkill -f keepalive.sh

set -u

SERVER_DIR="$(cd "$(dirname "$0")" && pwd)"

# Fix #2: use $HOME/.arena/logs instead of /tmp (Termux /tmp permission issues)
LOG_DIR="$HOME/.arena/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="${LOG_FILE:-$LOG_DIR/keepalive.log}"
SERVER_LOG="${SERVER_LOG:-$LOG_DIR/arena-server.log}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"  # seconds
MAX_BACKOFF=60
MAX_RESTART_PER_HOUR=10
backoff=5

# Kiwi package name (Kiwi Browser từ Play Store / APK)
# Kiểm tra: pm list packages | grep -i kiwi
KIWI_PACKAGE="${KIWI_PACKAGE:-com.kiwibrowser.browser}"
KIWI_ACTIVITY="com.google.android.apps.chrome.Main"

# Restart tracking
declare -A restart_counts
current_hour=$(date +%H)

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ── Log rotation — fix #9 ──────────────────────────────────────────────────
rotate_logs() {
    local max_size=$((1024 * 1024))  # 1MB
    for f in "$LOG_FILE" "$SERVER_LOG"; do
        if [ -f "$f" ]; then
            local size
            size=$(stat -c%s "$f" 2>/dev/null || echo 0)
            if [ "$size" -gt "$max_size" ]; then
                # Rotate: .log → .log.1, .log.1 → .log.2 (keep 3 backups)
                for i in 2 1; do
                    [ -f "${f}.${i}" ] && mv "${f}.${i}" "${f}.$((i+1))"
                done
                mv "$f" "${f}.1" 2>/dev/null || true
                : > "$f"  # truncate
                log "Log rotated: $f (was ${size} bytes)"
            fi
        fi
    done
}

# ── Disk space check — fix #10 ─────────────────────────────────────────────
check_disk_space() {
    local min_mb=50  # warn if free < 50MB
    local free_mb
    # df -k returns 1K blocks, divide by 1024 for MB
    free_mb=$(df -k /tmp 2>/dev/null | awk 'NR==2 {print int($4/1024)}')
    if [ -n "$free_mb" ] && [ "$free_mb" -lt "$min_mb" ]; then
        log "⚠ Disk space low: ${free_mb}MB free in /tmp"
        # Auto-cleanup old rotated logs
        find /tmp -name "keepalive.log.*" -mtime +7 -delete 2>/dev/null || true
        find /tmp -name "arena-server.log.*" -mtime +7 -delete 2>/dev/null || true
        log "Cleaned up old log backups"
    fi
}

# ── Setup ADB permissions (one-time) ───────────────────────────────────────
setup_adb() {
    log "Setting up ADB battery whitelist..."
    if command -v termux-open-uri >/dev/null 2>&1; then
        # Termux:API way
        :
    fi
    # Try ADB if available (Termux:com.android.tools)
    if command -v adb >/dev/null 2>&1; then
        adb shell dumpsys deviceidle whitelist "+$KIWI_PACKAGE" 2>/dev/null || true
        adb shell dumpsys deviceidle whitelist "+com.termux" 2>/dev/null || true
        adb shell cmd appops set "$KIWI_PACKAGE" RUN_IN_BACKGROUND allow 2>/dev/null || true
        adb shell cmd appops set "$KIWI_PACKAGE" RUN_ANY_IN_BACKGROUND allow 2>/dev/null || true
        log "ADB whitelist applied"
    else
        log "ADB not available. Manual setup needed:"
        log "  Settings → Apps → Kiwi → Battery → Unrestricted"
        log "  Settings → Apps → Termux → Battery → Unrestricted"
    fi
}

# ── Check server health ────────────────────────────────────────────────────
check_server() {
    local resp
    resp=$(curl -s -m 5 http://127.0.0.1:8000/health 2>/dev/null)
    if echo "$resp" | grep -q '"status":"ok"'; then
        return 0
    fi
    return 1
}

restart_server() {
    local hour=$(date +%H)
    if [ "$hour" != "$current_hour" ]; then
        current_hour="$hour"
        restart_counts[server]=0
    fi
    restart_counts[server]=$(( ${restart_counts[server]:-0} + 1 ))
    if [ "${restart_counts[server]:-0}" -gt "$MAX_RESTART_PER_HOUR" ]; then
        log "⚠ Server restart limit exceeded (${restart_counts[server]} in hour) — NOT restarting, investigate"
        send_alert "🚨 Arena: Server restart limit exceeded (${restart_counts[server]}/hour) — manual intervention needed"
        return 1
    fi
    log "→ Restarting server (attempt ${restart_counts[server]}/$MAX_RESTART_PER_HOUR this hour)..."
    send_alert "⚠ Arena: Server down, restarting (attempt ${restart_counts[server]}/$MAX_RESTART_PER_HOUR)"
    pkill -f "python3 main.py" 2>/dev/null
    sleep 2
    cd "$SERVER_DIR"
    nohup python3 main.py >> "$SERVER_LOG" 2>&1 &
    sleep 5
    if check_server; then
        log "✓ Server restarted OK"
        return 0
    else
        log "✗ Server still failing after restart"
        send_alert "🚨 Arena: Server restart FAILED — still down"
        return 1
    fi
}

# ── Check Kiwi Browser process — KHÔNG relaunch nếu chưa chạy ─────────────
# Logic vận hành đúng:
#   1. User mở Kiwi + extension + login arena.ai (manual, 1 lần)
#   2. User chạy arena start → server start
#   3. Extension auto-connect tới server (Kiwi đang chạy)
# Server KHÔNG proactively relaunch Kiwi vì:
#   - Sẽ che game khi user đang chơi
#   - Tab arena.ai cần user login, không thể auto
# Chỉ log warning nếu Kiwi không chạy, user tự mở.
check_kiwi() {
    if pgrep -f "$KIWI_PACKAGE" >/dev/null 2>&1; then
        return 0
    fi
    if command -v dumpsys >/dev/null 2>&1; then
        if dumpsys activity activities 2>/dev/null | grep -q "$KIWI_PACKAGE"; then
            return 0
        fi
    fi
    return 1
}

# KHÔNG tự relaunch Kiwi — chỉ log warning
warn_kiwi_down() {
    log "⚠ Kiwi Browser không chạy — extension sẽ không kết nối được"
    log "  → Mở Kiwi Browser + extension + tab arena.ai (login) thủ công"
    log "  → Server sẽ tự nhận khi extension connect"
    # Alert 1 lần, không spam
    if [ -z "${KIWI_WARNED:-}" ]; then
        send_alert "⚠ Arena: Kiwi Browser không chạy — mở Kiwi + extension để server hoạt động"
        KIWI_WARNED=1
    fi
}

# Reset warning khi Kiwi quay lại
reset_kiwi_warning() {
    KIWI_WARNED=""
}

# ── Check extension connection ─────────────────────────────────────────────
check_extension() {
    local resp
    resp=$(curl -s -m 5 http://127.0.0.1:8000/admin/broker 2>/dev/null)
    if [ -z "$resp" ]; then
        return 1
    fi
    if echo "$resp" | grep -q '"extension_connected":true'; then
        return 0
    fi
    return 1
}

# ── Thermal + battery check — fix #14, #32 ────────────────────────────────
check_thermal() {
    local battery_level=-1
    local is_charging=0

    # Termux:API sensors
    if command -v termux-battery-status >/dev/null 2>&1; then
        local battery_json
        battery_json=$(termux-battery-status 2>/dev/null)
        if [ -n "$battery_json" ]; then
            battery_level=$(echo "$battery_json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('percentage', -1))" 2>/dev/null)
            is_charging=$(echo "$battery_json" | python3 -c "import json,sys; print(1 if json.load(sys.stdin).get('status')=='charging' else 0)" 2>/dev/null)
        fi
    fi

    # Alert on low battery
    if [ "$battery_level" != "-1" ] && [ -n "$battery_level" ]; then
        if [ "$battery_level" -lt 15 ]; then
            log "⚠ Battery low: ${battery_level}% — phone may die soon"
            send_alert "⚠ Arena: Battery ${battery_level}% — phone may die"
        fi
        log "Battery: ${battery_level}% (charging=$is_charging)"

        # Adaptive interval — fix #32: slower checks when low battery + not charging
        if [ "$battery_level" -lt 30 ] && [ "$is_charging" = "0" ]; then
            ADAPTIVE_INTERVAL=120  # 2 min instead of 30s
            log "  Low battery mode: check interval → ${ADAPTIVE_INTERVAL}s"
        else
            ADAPTIVE_INTERVAL=$CHECK_INTERVAL
        fi
    else
        ADAPTIVE_INTERVAL=$CHECK_INTERVAL
    fi

    # CPU temp (root usually required, try anyway)
    if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
        local temp
        temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
        if [ -n "$temp" ]; then
            local temp_c=$((temp / 1000))
            if [ "$temp_c" -gt 70 ]; then
                log "⚠ CPU temp high: ${temp_c}°C"
                send_alert "⚠ Arena: CPU temp ${temp_c}°C — too hot"
            fi
        fi
    fi
}

# ── Alert webhook — fix #33 ────────────────────────────────────────────────
send_alert() {
    local message="$1"
    local webhook_url="${ALERT_WEBHOOK_URL:-}"

    if [ -z "$webhook_url" ]; then
        return 0  # No webhook configured, skip
    fi

    # Try Discord format first (most common)
    if [[ "$webhook_url" == *"discord.com"* ]]; then
        curl -s -m 5 -X POST "$webhook_url" \
            -H "Content-Type: application/json" \
            -d "{\"content\":\"$(echo "$message" | sed 's/"/\\"/g')\"}" 2>/dev/null || true
    # Telegram format
    elif [[ "$webhook_url" == *"telegram.org"* ]]; then
        curl -s -m 5 -X POST "$webhook_url" \
            -H "Content-Type: application/json" \
            -d "{\"text\":\"$(echo "$message" | sed 's/"/\\"/g')\"}" 2>/dev/null || true
    # Generic webhook
    else
        curl -s -m 5 -X POST "$webhook_url" \
            -H "Content-Type: application/json" \
            -d "{\"message\":\"$(echo "$message" | sed 's/"/\\"/g')\",\"source\":\"arena-web2api\"}" 2>/dev/null || true
    fi

    log "Alert sent: $message"
}

# ── Main loop ──────────────────────────────────────────────────────────────
main() {
    log "🚀 keepalive.sh starting — checks every ${CHECK_INTERVAL}s"
    log "   Server dir: $SERVER_DIR"
    log "   Log: $LOG_FILE"
    log "   Kiwi package: $KIWI_PACKAGE"
    setup_adb

    # Init adaptive interval
    ADAPTIVE_INTERVAL=$CHECK_INTERVAL

    while true; do
        # 1. Server check
        if ! check_server; then
            log "✗ Server down — restarting"
            restart_server
        fi

        # 2. Kiwi check — chỉ warn, không relaunch
        if ! check_kiwi; then
            warn_kiwi_down
        else
            reset_kiwi_warning
        fi

        # 3. Extension connection check
        if check_server; then
            if ! check_extension; then
                log "⚠ Extension not connected to broker (tab may be closed or Kiwi in background)"
                # Don't relaunch here — extension has its own auto-reopen logic
                # Just log for visibility
            fi
        fi

        # 4. Thermal + battery (every 5 min) — also sets ADAPTIVE_INTERVAL
        if [ $((SECONDS % 300)) -lt $ADAPTIVE_INTERVAL ]; then
            check_thermal
        fi

        # 5. Log rotation (every 5 min) — fix #9
        if [ $((SECONDS % 300)) -lt $ADAPTIVE_INTERVAL ]; then
            rotate_logs
        fi

        # 6. Disk space check (every 10 min) — fix #10
        if [ $((SECONDS % 600)) -lt $ADAPTIVE_INTERVAL ]; then
            check_disk_space
        fi

        # Use adaptive interval (slower when low battery)
        sleep "$ADAPTIVE_INTERVAL"
    done
}

# Handle signals
trap 'log "🛑 keepalive.sh stopping..."; exit 0' SIGTERM SIGINT

main "$@"
