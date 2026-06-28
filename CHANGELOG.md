# Changelog

## [3.1.0] — 2026-06-28

Strict code review (đọc từng hàm, tìm bug/race/dead code/security) → fix 11 vấn đề.
Chi tiết xem `REVIEW.md`.

### Fixed (Critical)
- **B1** Circuit breaker không mark success khi async generator bị hủy sớm
  (GeneratorExit). Giờ phân biệt: success / neutral (disconnect) / failure rõ ràng.
- **B2** Circuit breaker HALF_OPEN không giới hạn probe → giờ enforce `CB_HALF_OPEN_MAX`.
- **B3** Stream rỗng được coi thành công → giờ raise `ArenaServerError` (retry + track).
- **B4** Idempotency TOCTOU race → single-flight (lock per-key, request thứ 2 chờ).

### Fixed (High)
- **B5/B6** Xóa dead code: dedup module, `get_conversation_lock`, `_NullLock`, `_conv_locks`.
- **B7** Auth timing attack → `secrets.compare_digest` (constant-time).
- **B8** ConversationStore race sync/async → `threading.Lock` cho sync accessors.
- **B9** persist() không atomic → ghi temp + `os.replace` (atomic).

### Fixed (Medium)
- **B10** CookiePool round-robin không ổn định → least-recently-used (LRU).
- **B11** TokenBucket over-admit sau sleep → re-check token.
- **B12** TPM rate limiting chưa wire vào route → `acquire_tokens(prompt_tokens)`.

### Tests
- Thêm 16 regression test cho từng bug (B1–B12). Tổng **73 test cases** pass.

## [3.0.0] — 2026-06-28
Tool/function calling, API key auth, concurrency gate, idempotency, auto-reconnect
SSE, request tracing, vision, readiness probe, Docker + CI.

## [2.1.0] — 2026-06-27
Dynamic UUID, multi-turn thật, SSE parser mạnh, cookie pool, circuit breaker,
rate limiter, metrics, conversation store.

## [2.0.0] — 2026-06-27
Rebuild 19 file, OpenAI-compatible. *(Codex review: 6.5/10)*

## [1.0.0] — 2026-06-26
Bản mobile đầu tiên (cookie thủ công + httpx).
