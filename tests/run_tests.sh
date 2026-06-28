#!/bin/bash
# Chạy toàn bộ test suite
set -e
cd "$(dirname "$0")/.."
echo "━━━ SSE + backoff unit tests ━━━"
python3 tests/test_sse.py
echo ""
echo "━━━ Pipeline integration tests ━━━"
python3 tests/test_pipeline.py
