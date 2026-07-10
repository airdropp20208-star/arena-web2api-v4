#!/usr/bin/env bash
# auto-setup.sh — One-click setup cho arena-web2api
#
# Cách dùng:
#   pkg install git -y
#   git clone https://github.com/airdropp20208-star/arena-web2api-v4.git
#   cd arena-web2api-v4
#   bash auto-setup.sh
#
# Script tự động:
#   1. Cài deps (Python, pip, curl)
#   2. Cài Python packages
#   3. Tạo .env (cần paste cookie)
#   4. Cài 'arena' command
#   5. Tải extension zip (cài vào Kiwi)
#   6. Hướng dẫn start

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${CYAN}ℹ${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  arena-web2api — Auto Setup"
echo "═══════════════════════════════════════════════════════"
echo ""

# 1. Cài deps
info "Bước 1/5: Cài dependencies..."
pkg update -y -qq 2>/dev/null || true
pkg install -y python python-pip curl git -qq 2>/dev/null || {
    # Non-Termux (Linux)
    apt-get update -qq 2>/dev/null || true
    apt-get install -y python3 python3-pip curl git -qq 2>/dev/null || true
}
log "Dependencies OK"

# 2. Cài Python packages
info "Bước 2/5: Cài Python packages..."
pip install -r requirements.txt --quiet --break-system-packages 2>/dev/null || {
    pip3 install -r requirements.txt --quiet 2>/dev/null || {
        warn "pip install fail — thử từng package"
        for pkg in $(cat requirements.txt); do
            pip install "$pkg" --quiet --break-system-packages 2>/dev/null || pip3 install "$pkg" --quiet 2>/dev/null || true
        done
    }
}
log "Python packages OK"

# 3. Tạo .env
info "Bước 3/5: Cấu hình .env..."
if [ -f .env ]; then
    warn ".env đã tồn tại. Giữ nguyên."
else
    cat > .env << 'ENVEOF'
# Arena cookie (BẮT BUỘC — xem hướng dẫn bên dưới)
# Cách lấy: Mở Kiwi → arena.ai → F12 → Application → Cookies
# Copy arena-auth-prod-v1.0 + arena-auth-prod-v1.1, gói thành JSON:
ARENA_AUTH_COOKIE=
CF_CLEARANCE=

# reCAPTCHA solver
RECAPTCHA_SOLVER=extension
RECAPTCHA_SITE_KEY=6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0
RECAPTCHA_ACTION=chat_submit
RECAPTCHA_TOKEN_TTL=90
RECAPTCHA_SOLVE_TIMEOUT=30

# Server
HOST=127.0.0.1
PORT=8000
LOG_LEVEL=INFO
DEBUG=false

# Cookie pool
COOKIE_HEALTH_TTL=300
COOKIE_FAIL_THRESHOLD=3
COOKIE_AUTO_REFRESH=true

# Retry
RETRY_ATTEMPTS=3
RETRY_BASE_DELAY=1.5
RETRY_MAX_DELAY=30
RETRY_JITTER=0.3
REQUEST_TIMEOUT=120
CONNECT_TIMEOUT=15

# Conversation
CONVERSATION_TTL=7200
CONVERSATION_MAX_TURNS=50
CONVERSATION_STORE_FILE=./data/conversations.json

# Circuit breaker
CB_ENABLED=true
CB_FAILURE_THRESHOLD=5
CB_COOLDOWN=30
CB_HALF_OPEN_MAX=1

# Metrics
METRICS_ENABLED=true
ENVEOF
    log ".env tạo xong"
    echo ""
    echo "  ┌──────────────────────────────────────────────────┐"
    echo "  │  CẦN EDIT .env — ĐIỀN COOKIE                    │"
    echo "  │                                                  │"
    echo "  │  Cách 1: nano .env                               │"
    echo "  │  Cách 2: arena setup (interactive)               │"
    echo "  │                                                  │"
    echo "  │  Cookie lấy từ:                                  │"
    echo "  │  - Kiwi → arena.ai → F12 → Application → Cookies │"
    echo "  │  - Hoặc extension popup 'Test Cookies'           │"
    echo "  └──────────────────────────────────────────────────┘"
    echo ""
fi

# 4. Cài arena CLI
info "Bước 4/5: Cài 'arena' command..."
chmod +x arena broker-only.sh keepalive.sh install.sh precheck.py 2>/dev/null || true
bash install.sh 2>/dev/null || {
    # Fallback: manual symlink
    ln -sf "$(pwd)/arena" "$HOME/.local/bin/arena" 2>/dev/null || true
    ln -sf "$(pwd)/arena" /data/data/com.termux/files/usr/bin/arena 2>/dev/null || true
}
log "'arena' command OK"

# 5. Tải extension
info "Bước 5/5: Tải extension zip..."
EXTENSION_ZIP="arena-extension.zip"
curl -sL -o "$EXTENSION_ZIP" \
  "https://github.com/airdropp20208-star/arena-web2api-v4/releases/download/extension-v2.0.0/arena-extension-v2.1.0.zip" \
  2>/dev/null && log "Extension zip tải xong: $EXTENSION_ZIP" || {
    warn "Không tải được extension zip — copy từ thư mục extension/ trong repo"
    cp -r extension/ arena-extension-folder 2>/dev/null || true
}

# Done
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✅ SETUP HOÀN TẤT"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Bước tiếp theo:"
echo ""
echo "  1. EDIT .env — điền cookie:"
echo "     nano .env"
echo "     # Hoặc: arena setup"
echo ""
echo "  2. CÀI EXTENSION trên Kiwi Browser:"
echo "     - Mở Kiwi → kiwi://extensions"
echo "     - Developer mode = ON"
echo "     - Click '+' → chọn $EXTENSION_ZIP"
echo ""
echo "  3. MỞ TAB arena.ai trên Kiwi → login"
echo ""
echo "  4. START SERVER:"
echo "     arena start"
echo ""
echo "  5. CHECK STATUS:"
echo "     arena status"
echo "     # → Extension: ✓ connected (cached: ✓)"
echo ""
echo "  6. TEST CHAT:"
echo '     curl -X POST http://127.0.0.1:8000/v1/chat/completions \'
echo '       -H "Content-Type: application/json" \'
echo '       -d '"'"'{"model":"arena-battle","messages":[{"role":"user","content":"hello"}]}'"'"''
echo ""
echo "═══════════════════════════════════════════════════════"
