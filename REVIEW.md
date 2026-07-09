# Strict Code Review — arena-web2api v3.0.0

Đọc từng hàm, tìm bug, race condition, bottleneck, dead code, lỗ hổng bảo mật.
Mức độ: 🔴 Critical · 🟠 High · 🟡 Medium · 🟢 Low

---

## 🔴 CRITICAL

### B1 — Circuit breaker không mark success khi stream bị hủy sớm
**File:** `src/client.py:_stream_grounded`

```python
try:
    async for ev in self._stream_with_retry(...):
        yield ev
except (...):
    await breaker.failure(); raise
else:
    await breaker.success()   # ← KHÔNG chạy khi client ngắt stream
```

Đây là async generator. Khi `StreamingResponse` kết thúc (client disconnect, route
return trước khi exhaust), Python gọi `aclose()` → generator nhận `GeneratorExit`
tại điểm yield → `except Exception` **không bắt được** (GeneratorExit kế thừa
BaseException) → `else` cũng **không chạy** → `breaker.success()` bị bỏ qua.

**Hậu quả:** Nếu 5 request success nhưng client disconnect sớm mỗi lần, failure
count không reset được, breaker có thể stuck hoặc trip sai.

**Fix:** Dùng `try/finally` với cờ trạng thái, xử lý cả `GeneratorExit`.

---

### B2 — Circuit breaker HALF_OPEN không giới hạn probe
**File:** `src/circuit_breaker.py`

`CB_HALF_OPEN_MAX` được khai báo trong config nhưng **không import, không dùng**
trong `circuit_breaker.py` (bị ruff xóa khi tự sắp xếp import). Khi `OPEN →
HALF_OPEN`, **mọi** request đều đi qua không giới hạn → không phải circuit
breaker chuẩn (spec yêu cầu cho tối đa N probe requests).

**Fix:** Đếm probe trong HALF_OPEN, reject khi vượt `CB_HALF_OPEN_MAX`.

---

### B3 — Stream rỗng được coi thành công
**File:** `src/client.py:_stream_attempt`

```python
if not started:
    logger.warning("Arena stream trả về rỗng.")
    # KHÔNG raise → return bình thường → breaker.success()
```

Stream trả về 0 event = dấu hiệu lỗi upstream rõ ràng. Hiện được coi thành công
→ breaker không bao giờ trip ngay cả khi Arena trả rỗng liên tục, và cũng không retry.

**Fix:** Raise `ArenaServerError` để kích hoạt retry + breaker tracking.

---

### B4 — Idempotency TOCTOU race condition
**File:** `src/idempotency.py` + `src/routes/chat.py`

```python
cached = await idempotency.get(key)   # CHECK
if cached: return cached
# ... upstream call ...
await idempotency.put(key, response)  # SET
```

Giữa `get` và `put` có await point. 2 request đồng thời cùng `Idempotency-Key`
→ cả 2 chạy upstream → idempotency bị phá hoàn toàn.

**Fix:** Single-flight: lock per-key, request thứ 2 chờ request thứ 1 xong rồi
nhận cached result.

---

## 🟠 HIGH

### B5 — Dedup module là dead code + memory leak nếu dùng
**File:** `src/concurrency.py`

`acquire_dedup`/`release_dedup`/`_inflight` **không được gọi ở route nào**. Nếu
dùng: exception/cancel giữa `acquire` và `release` → key kẹt trong `_inflight`
forever (memory leak). Không có `try/finally`.

**Fix:** Xóa dead code.

### B6 — `get_conversation_lock` / `_NullLock` / `_conv_locks` là dead code
**File:** `src/concurrency.py`

Không gọi ở đâu. `_conv_locks` dict cũng không bao giờ evict → leak.

**Fix:** Xóa.

### B7 — Timing attack trong API key auth
**File:** `src/auth.py`

```python
if not key or key not in API_KEYS:
```

`key in set` dùng hash lookup → timing phụ thuộc vào prefix match → rò rỉ thông
tin về key hợp lệ (side-channel). Với local use thì OK, nhưng khi expose public
là lỗ hổng thật.

**Fix:** `secrets.compare_digest` so sánh constant-time từng key.

### B8 — ConversationStore: race giữa sync accessor và async cleanup
**File:** `src/conversation_store.py`

`put_sync` (không lock) gọi `_cleanup_locked` rebuild dict, trong khi
`cleanup()` async (có lock) cũng rebuild. 2 coroutine concurrent → conflict.

**Fix:** Dùng `threading.Lock` đồng bộ thật cho sync accessors (vì không có
await point, asyncio.Lock vô dụng).

### B9 — persist() không atomic
**File:** `src/conversation_store.py`

Ghi thẳng file → crash giữa chừng = file JSON corrupt (half-written).

**Fix:** Ghi `.tmp` rồi `os.replace()` (atomic trên cùng filesystem).

---

## 🟡 MEDIUM

### B10 — CookiePool round-robin không ổn định
**File:** `src/cookie_pool.py`

`self._rr` tăng vô hạn, `idx = self._rr % n` với `n = len(healthy)` thay đổi
khi cookie healthy/unhealthy → phân phối không đều, có thể lặp hoặc skip cookie.

**Fix:** Least-recently-used (chọn healthy có `last_used` nhỏ nhất).

### B11 — TokenBucket có thể over-admit sau sleep
**File:** `src/rate_limiter.py`

Sau `asyncio.sleep`, re-acquire lock rồi `tokens = max(0, tokens - n)` → nếu
request khác vắt kiệt bucket lúc sleep, vẫn admit → vượt rate tạm thời.

**Fix:** Re-check; nếu vẫn thiếu → reject hoặc đợi thêm.

### B12 — TPM rate limiting không bao giờ được wire vào
**File:** `src/routes/chat.py`

Routes chỉ gọi `limiter.acquire_request()` (RPM). `acquire_tokens()` (TPM) không
bao giờ được gọi → TPM config vô dụng.

**Fix:** Wire `acquire_tokens(prompt_tokens)` vào chat route.

---

## 🟢 LOW

- **B13:** `_by_key` field trong ConversationStore là alias thừa (= `self._convs`).
- **B14:** `arena_session_id` field trong Conversation không bao giờ dùng trong payload → dead field.
- **B15:** `acquire_tokens` TPM code path chưa có test.

---

## Kết luận trước khi fix

Kiến trúc đầy đủ (9.5) nhưng **hiện thực hóa còn khoảng cách** — đúng như reviewer
nghi ngờ. 4 bug Critical + 5 High là vấn đề thật sẽ gây hành vi sai dưới tải hoặc
khi upstream lỗi. Fix hết → mới đáng 8.8–9.5.
