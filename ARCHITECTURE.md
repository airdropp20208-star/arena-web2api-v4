# Kiến trúc — arena-web2api v2.1

## Tổng quan luồng request

```
Client (OpenAI SDK / curl)
        │  POST /v1/chat/completions
        ▼
┌──────────────────────────────────────────────────────────┐
│  FastAPI route  (routes/chat.py)                          │
│   • parse ChatRequest                                     │
│   • extract attachments (vision)                          │
│   • ConversationManager.plan_turn()  ← multi-turn logic   │
└──────────────────────────────────────────────────────────┘
        │  TurnPlan (conversation + send_content + attachments)
        ▼
┌──────────────────────────────────────────────────────────┐
│  ArenaClient  (client.py)                                 │
│   • RateLimiter.acquire  (token bucket)                   │
│   • CircuitBreaker.check (CLOSED/OPEN/HALF_OPEN)          │
│   • build payload (modelAId từ ModelRegistry)             │
│   • _stream_with_retry  (backoff + jitter, đổi cookie/proxy) │
└──────────────────────────────────────────────────────────┘
        │  POST /nextjs-api/stream/create-evaluation
        ▼
┌──────────────────────────────────────────────────────────┐
│  arena.ai  (SSE stream)                                   │
└──────────────────────────────────────────────────────────┘
        │  text chunks
        ▼
┌──────────────────────────────────────────────────────────┐
│  SSEDecoder → parse_arena_event  → ArenaEvent             │
│   (content / role / finish_reason / model_index / reveal) │
└──────────────────────────────────────────────────────────┘
        │  ArenaEvent
        ▼
   route build OpenAI chunk  →  Client
   │
   └► commit_response(plan, content) → ConversationStore
   └► metrics.record(...)
```

## Các module chính

### Conversation (multi-turn thật)
`conversation.py` + `conversation_store.py`

- **Fingerprint**: `SHA1(model + role:content cho mỗi message)`.
- **Prefix matching**: request mới có `prefix_key = fp(messages[:-1])`.
  Nếu khớp `full_key` của 1 conversation đang sống → **continuation**: gửi
  CHỈ `messages[-1]`, tái dùng `conversationId`.
- **Mới**: gửi history flattened làm message đầu; đăng ký conversation.
- **Commit**: sau khi có response, append `(user, assistant)` → re-key để lượt
  sau match. Xoá key cũ tránh trùng.

### Model registry (dynamic UUID)
`model_registry.py` — fetch `/nextjs-api/models`, map `name → id`, cache TTL
(`MODEL_REGISTRY_TTL`), refresh loop nền, fallback tĩnh. Giải quyết UUID giả.

### SSE parser
`sse_parser.py` — 2 tầng:
1. `SSEDecoder`: đúng wire-protocol (buffer partial, multi-line data, comment,
   event/id/retry, dispatch trên dòng trống).
2. `parse_arena_event`: chịu nhiều JSON shape — `choices[].delta`, `content`,
   `text`, `message`, `finish_reason`, `model_index` (0/1/a/b/modelA…),
   `type=reveal`, `error`.

### Resilience
| Module | Vai trò |
|--------|---------|
| `cookie_pool.py` | Nhiều cookie, round-robin, fail threshold, health-check |
| `circuit_breaker.py` | Trip khi N fail liên tiếp, cooldown → half-open |
| `rate_limiter.py` | Token bucket RPM + TPM |
| `client._stream_with_retry` | backoff+jitter, status-aware, đổi cookie/proxy mỗi retry |

### Lớp lỗi
`errors.py` — `ArenaWeb2APIError` base → `ArenaError(status, retryable)` →
`ArenaAuthError`/`ArenaRateLimitError`/`ArenaServerError`; cộng
`NoCookiesError`, `ModelNotResolvedError`, `CircuitOpenError`, `RateLimitedError`,
`SSEParseError`. Route map `.status` → HTTP.

## Singleton dùng chung
`client`, `registry`, `store`, `breaker`, `limiter`, `metrics`, `manager` —
khởi tạo lazy / trong lifespan.

## Tại sao không dùng browser automation?
Chạy trên điện thoại (Userland Ubuntu) → browser (Byparr/Camoufox) quá nặng.
Thay vào đó: cookie thủ công + httpx gọi thẳng API nội bộ Arena.
