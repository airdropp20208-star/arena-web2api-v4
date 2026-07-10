#!/usr/bin/env bash
# Termux:Boot script — chạy tự động khi phone restart.
#
# Cài đặt:
#   1. Cài app "Termux:Boot" từ F-Droid (KHÔNG có trên Play Store)
#      https://f-droid.org/packages/com.termux.boot/
#   2. Mở Termux:Boot 1 lần (cần để Android grant permission)
#   3. Copy script này tới ~/.termux/boot/arena-web2api
#      mkdir -p ~/.termux/boot
#      cp termux-boot/start.sh ~/.termux/boot/arena-web2api
#      chmod +x ~/.termux/boot/arena-web2api
#
# Sau khi phone restart, Termux:Boot sẽ chạy script này tự động.

set -u

# Đợi 30s sau boot để network ready
sleep 30

# Acquire wakelock — phone không sleep khi Termux chạy
termux-wake-lock 2>/dev/null || true

# Đường dẫn tới arena-web2api (CHỈNH SỬA CHO ĐÚNG)
ARENA_DIR="${ARENA_DIR:-$HOME/arena-web2api-v4}"

if [ ! -d "$ARENA_DIR" ]; then
    echo "[$(date)] ❌ $ARENA_DIR not found, skip auto-start" >> /tmp/termux-boot.log
    exit 1
fi

cd "$ARENA_DIR"

# Start keepalive (nó sẽ start server nếu cần)
nohup bash keepalive.sh > /tmp/keepalive.log 2>&1 &
disown

echo "[$(date)] ✅ arena-web2api auto-started via Termux:Boot" >> /tmp/termux-boot.log
