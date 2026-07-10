#!/data/data/com.termux/files/usr/bin/bash
# broker-only.sh — chạy WS broker trên port 8765, KHÔNG start server HTTP
#
# Dùng khi user muốn tách rời:
#   - Session 1: arena broker start  (chạy broker.sh, giữ nguyên)
#   - Session 2: arena start         (chạy server HTTP, connect broker)
#
# Broker này cần thiết vì:
#   - Extension Kiwi connect tới ws://localhost:8765
#   - Server HTTP connect tới broker qua in-process (cùng Python process)
#   - Nếu server crash, broker vẫn chạy → extension không mất kết nối

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Fix #2: use $HOME/.arena/logs instead of /tmp (Termux /tmp permission issues)
LOG_DIR="$HOME/.arena/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="${LOG_FILE:-$LOG_DIR/arena-broker.log}"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[broker]${NC} $*"; }
warn() { echo -e "${YELLOW}[broker]${NC} $*"; }
err()  { echo -e "${RED}[broker]${NC} $*" >&2; }

# Load .env
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

HOST="${TOKEN_BROKER_HOST:-0.0.0.0}"
PORT="${TOKEN_BROKER_PORT:-8765}"

log "Starting WS broker only (no HTTP server)"
log "  Bind: ws://${HOST}:${PORT}"
log "  Log: ${LOG_FILE}"
log "  PID: $$"
log ""
log "Extension (Kiwi Browser) connect tới: ws://localhost:${PORT}"
log ""
log "Press Ctrl-C to stop. Server HTTP chạy ở session khác: 'arena start'"
log ""

# Run broker in foreground (this session)
# Note: don't use tee if /tmp not writable — log dir already created
exec python3 -c "
import asyncio
import sys
sys.path.insert(0, '.')
from src.token_broker import broker

async def main():
    await broker.start(host='${HOST}', port=${PORT})
    print(f'[broker] Listening ws://${HOST}:${PORT}')
    print(f'[broker] Extension connect: ws://localhost:${PORT}')
    print(f'[broker] Waiting for extension + server...')
    print()
    # Keep running forever
    try:
        while True:
            await asyncio.sleep(60)
            snap = broker.snapshot()
            print(f'[broker] Status: extension={snap[\"extension_connected\"]}, '
                  f'tokens={snap[\"token_count\"]}, '
                  f'connects={snap[\"connect_count\"]}')
    except (KeyboardInterrupt, asyncio.CancelledError):
        print()
        print('[broker] Shutting down...')
        await broker.stop()

asyncio.run(main())
"
