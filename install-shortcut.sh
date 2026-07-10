#!/usr/bin/env bash
# install-shortcut.sh — Tạo shortcut "Arena" trên home screen
#
# Cài Termux:Widget từ F-Droid:
#   https://f-droid.org/packages/com.termux.widget/
#
# Sau đó chạy:
#   bash install-shortcut.sh
#
# Trên home screen: long-press → Widget → Termux:Widget → "Arena"

set -u

GREEN='\033[0;32m'
NC='\033[0m'
log() { echo -e "${GREEN}✓${NC} $*"; }

ARENA_DIR="${ARENA_DIR:-$HOME/arena-web2api-v4}"
SHORTCUT_DIR="$HOME/.shortcuts"

mkdir -p "$SHORTCUT_DIR"

# Tạo shortcut script
cat > "$SHORTCUT_DIR/Arena" << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
cd ~/arena-web2api-v4
bash arena-go
EOF
chmod +x "$SHORTCUT_DIR/Arena"

log "Shortcut 'Arena' tạo xong tại $SHORTCUT_DIR/Arena"
log ""
log "Cách tạo icon trên home screen:"
log "  1. Cài Termux:Widget (F-Droid)"
log "  2. Long-press desktop → Widget"
log "  3. Chọn 'Termux:Widget' → 'Termux shortcut'"
log "  4. Chọn 'Arena'"
log ""
log "Sau đó click icon 'Arena' trên desktop = 1 click start tất cả"
