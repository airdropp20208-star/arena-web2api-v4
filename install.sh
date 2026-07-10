#!/usr/bin/env bash
# install.sh — cài `arena` command globally
#
# Chạy 1 lần:
#   cd arena-web2api-v4
#   bash install.sh
#
# Sau đó dùng ở bất kỳ đâu:
#   arena setup
#   arena start
#   arena status

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARENA_SCRIPT="$SCRIPT_DIR/arena"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[install]${NC} $*"; }
warn() { echo -e "${YELLOW}[install]${NC} $*"; }
err()  { echo -e "${RED}[install]${NC} $*" >&2; }

# Verify arena script exists
if [ ! -f "$ARENA_SCRIPT" ]; then
    err "Không tìm thấy arena script tại: $ARENA_SCRIPT"
    exit 1
fi

chmod +x "$ARENA_SCRIPT"
log "✓ arena script executable"

# Tìm bin dir phù hợp
BIN_DIR=""
for candidate in /usr/local/bin "$PREFIX/bin" "$HOME/.local/bin" "$HOME/bin"; do
    if [ -d "$candidate" ] && [ -w "$candidate" ]; then
        BIN_DIR="$candidate"
        break
    fi
done

# Nếu chưa có dir, tạo ~/.local/bin
if [ -z "$BIN_DIR" ]; then
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
    warn "Created $BIN_DIR"
fi

# Tạo symlink
ln -sf "$ARENA_SCRIPT" "$BIN_DIR/arena"
log "✓ Symlink: $BIN_DIR/arena → $ARENA_SCRIPT"

# Check if BIN_DIR in PATH
case ":$PATH:" in
    *":$BIN_DIR:"*)
        log "✓ $BIN_DIR đã trong PATH"
        ;;
    *)
        warn "$BIN_DIR chưa trong PATH. Add vào shell config:"
        echo ""
        echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc"
        echo "  source ~/.bashrc"
        echo ""
        # Auto-add for convenience
        if [ -f "$HOME/.bashrc" ]; then
            echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$HOME/.bashrc"
            log "Auto-added to ~/.bashrc (reload: source ~/.bashrc)"
        fi
        ;;
esac

# Verify
echo ""
log "Verify:"
if command -v arena >/dev/null 2>&1; then
    echo "  ✓ 'arena' command available"
    echo ""
    echo "Test:"
    echo "  arena --help"
    echo ""
    echo "Setup:"
    echo "  arena setup"
    echo ""
    echo "Start:"
    echo "  arena start"
else
    warn "  'arena' chưa có trong PATH. Reload shell:"
    echo "  source ~/.bashrc"
    echo "  # hoặc mở Termux mới"
fi

echo ""
log "✓ Install hoàn tất"
