#!/bin/bash
# Chạy toàn bộ test suite
set -e
cd "$(dirname "$0")/.."

echo "━━━ SSE + backoff unit tests ━━━"
python3 tests/test_sse.py
echo ""
echo "━━━ Pipeline integration tests ━━━"
python3 tests/test_pipeline.py
echo ""
echo "━━━ Resilience + regression tests ━━━"
python3 tests/test_resilience.py
echo ""
echo "━━━ Tools/attachment unit tests ━━━"
python3 tests/test_tools.py
echo ""
echo "━━━ Tool calling integration tests ━━━"
python3 tests/test_tools_integration.py
echo ""
echo "━━━ reCAPTCHA solver tests ━━━"
python3 tests/test_recaptcha.py
echo ""
echo "━━━ Token broker (extension) tests ━━━"
python3 tests/test_token_broker.py
echo ""
echo "━━━ Cookie refresh + relogin tests ━━━"
python3 tests/test_cookie_refresh.py
echo ""
echo "━━━ Hardening tests (redact, token bucket, dedup, banned detection) ━━━"
python3 tests/test_hardening.py
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 TẤT CẢ TEST SUITES PASS"
