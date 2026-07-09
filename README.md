# arena-web2api

**Biến [arena.ai](https://arena.ai) thành OpenAI-compatible API — production-grade.**

Direct mode (chọn model cụ thể), Battle mode (2 model ẩn danh + reveal + vote),
streaming SSE, **multi-turn thật**, **dynamic UUID sync**, cookie pool, circuit breaker,
rate limiter, metrics.

> Dùng được ngay với OpenAI SDK, Cherry Studio, OpenWebUI, LangChain, v.v.

---

## ✨ Tính năng

| Nhóm | Chi tiết |
|------|----------|
| **OpenAI-compatible** | `/v1/chat/completions`, `/v1/models`, streaming SSE chuẩn |
| **Direct mode** | Chat với model cụ thể (Claude, GPT, Gemini, Grok, Arena Max…) |
| **Battle mode** | 2 model ẩn danh song song → reveal tên + vote (`/v1/battle`, `/v1/battle/vote`) |
| **Code / Webdev** | `/v1/code/completions` — Arena webdev mode, sinh code |
| **Image** | `/v1/image/completions` — Arena image generation |
| **Video** | `/v1/video/completions` — Arena video generation |
| **Search** | `/v1/search/completions` — Arena search mode |
| **Modality param** | Thêm `modality: chat|webdev|image|video|search` vào request body |
| **Multi-turn thật** | Tái dùng `conversationId` của Arena, gửi **incremental** (không ghép string) |
| **Dynamic UUID sync** | Tự fetch UUID model từ `/nextjs-api/models`, cache TTL, fallback tĩnh |
| **SSE parser mạnh** | đúng wire-protocol: multi-line data, comment, partial chunk, finish_reason, reveal |
| **Cookie pool** | Nhiều account xoay vòng + health-check + auto-refresh tuỳ chọn |
| **Retry thông minh** | backoff + jitter, status-aware (429/5xx), `Retry-After` |
| **Circuit breaker** | tự OPEN khi upstream lỗi liên tục, HALF_OPEN thử lại |
| **Rate limiter** | token bucket RPM + TPM |
| **Metrics** | request / token / latency / lỗi theo model (`/admin/metrics`) |
| **Proxy rotation** | pool proxy xoay vòng |
| **Tokenizer** | tiktoken (`o200k_base`) + fallback heuristic |
| **Vision hook** | `experimental_attachments` (image_url) đi qua payload |
| **Nhẹ** | Python + FastAPI, không Docker, không browser |

---

## 🚀 Cài đặt

```bash
git clone https://github.com/tenmay/arena-web2api
cd arena-web2api
bash setup.sh          # cài deps + tạo .env
nano .env              # điền cookie
python3 main.py
```

Yêu cầu: **Python ≥ 3.10**.

---

## 🍪 Lấy cookie (bắt buộc)

Arena dùng Cloudflare — cần cookie từ browser thật.

### Cách 1: Kiwi Browser (Android) — khuyên dùng
1. Cài **Kiwi Browser** → vào `https://arena.ai` → đăng nhập
2. Menu (⋮) → **Desktop site** → bật → Menu → **Developer tools**
3. Tab **Application** → **Cookies** → `https://arena.ai`
4. Copy:
   - `arena-auth-prod-v1` → `ARENA_AUTH_COOKIE`
   - `cf_clearance` → `CF_CLEARANCE`

### Cách 2: PC rồi copy sang
Chrome/Firefox → F12 → Application → Cookies → `arena.ai` → copy 2 cookie trên.

```env
ARENA_AUTH_COOKIE=eyJhbGci...
CF_CLEARANCE=abc123xyz...
```

### Nhiều account (cookie pool)
```env
COOKIE_POOL=auth1|cf1,auth2|cf2,auth3|cf3
COOKIE_AUTO_REFRESH=true
```
Server xoay vòng giữa các cookie, health-check định kỳ, tự cách ly cookie chết.

---

## 📡 Endpoints

| Method | Path | Mô tả |
|--------|------|-------|
| `GET`  | `/health` | Liveness |
| `GET`  | `/cookie-status` | Trạng thái cookie (legacy) |
| `GET`  | `/v1/models` | Danh sách model (dynamic) |
| `GET`  | `/v1/models/refresh` | Force refresh UUID map |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat (battle/direct) |
| `POST` | `/v1/code/completions` | Arena webdev mode (code generation) |
| `POST` | `/v1/image/completions` | Arena image generation mode |
| `POST` | `/v1/video/completions` | Arena video generation mode |
| `POST` | `/v1/search/completions` | Arena search mode |
| `POST` | `/v1/battle` | Battle mode (response tách biệt + reveal) |
| `POST` | `/v1/battle/vote` | Vote cho battle |
| `GET`  | `/admin/status` | Tổng quan hệ thống |
| `GET`  | `/admin/cookies` | Cookie pool snapshot |
| `POST` | `/admin/cookies/validate` | Health-check pool |
| `GET`  | `/admin/registry` | Model registry snapshot |
| `GET`  | `/admin/metrics` | Metrics theo model |
| `GET`  | `/admin/breaker` | Circuit breaker state |
| `POST` | `/admin/breaker/reset` | Reset breaker |
| `GET`  | `/admin/ratelimit` | Rate limiter state |
| `GET`  | `/admin/conversations` | Conversation store |

> Đặt `ADMIN_TOKEN` trong `.env` để bảo vệ các endpoint `/admin/*` (gửi header `X-Admin-Token`).

---

## 💬 Sử dụng

### Chat (Direct mode)
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-6",
       "messages":[{"role":"user","content":"Xin chào!"}]}'
```

### Code / Webdev mode
```bash
curl http://localhost:8000/v1/code/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Create a todo app with React"}]}'
```

### Image generation
```bash
curl http://localhost:8000/v1/image/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"A cat wearing a hat"}]}'
```

### Video generation
```bash
curl http://localhost:8000/v1/video/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"A sunset timelapse"}]}'
```

### Search
```bash
curl http://localhost:8000/v1/search/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Latest AI news"}]}'
```

### Or use modality parameter on /v1/chat/completions
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"modality":"webdev",
       "messages":[{"role":"user","content":"Build a dashboard"}]}'
```

### Battle mode
```bash
curl http://localhost:8000/v1/battle \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"AI là gì?"}]}'
# → { conversation_id, model_a:{content,model}, model_b:{...}, revealed:true }
```

### Streaming
```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","messages":[{"role":"user","content":"Kể chuyện"}],"stream":true}'
```

### OpenAI SDK
```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="none")
resp = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

### Cherry Studio / OpenWebUI
| Field | Value |
|-------|-------|
| Base URL | `http://localhost:8000/v1` |
| API Key | `none` |
| Model | chọn từ `/v1/models` |

---

## 🧠 Multi-turn hoạt động thế nào

OpenAI client luôn gửi **toàn bộ history** mỗi request. arena-web2api:

1. Hash `(model + history)`.
2. Nếu *prefix* (trừ message cuối) khớp 1 conversation đang sống → **lượt tiếp theo**:
   gửi **chỉ** message mới nhất, tái dùng `conversationId` → Arena giữ context thật.
3. Không khớp → conversation mới: gửi history flattened làm message đầu.

→ **Không còn ghép toàn bộ history thành 1 string mỗi turn.** Xem test `test_pipeline.py`
(assert: turn 2 gửi ít ký tự hơn turn 1).

---

## ⚙️ Cấu hình (`.env`)

Xem `.env.example` cho đầy đủ. Các nhóm chính:

```env
# Cookie
ARENA_AUTH_COOKIE=...        CF_CLEARANCE=...        COOKIE_POOL=auth|cf,...

# Retry            RETRY_ATTEMPTS, RETRY_BASE_DELAY, RETRY_MAX_DELAY, RETRY_JITTER
# Circuit breaker  CB_ENABLED, CB_FAILURE_THRESHOLD, CB_COOLDOWN
# Rate limit       RATE_LIMIT_ENABLED, RATE_LIMIT_RPM, RATE_LIMIT_TPM
# Registry         MODEL_REGISTRY_TTL, MODEL_REGISTRY_ON_STARTUP
# Conversation     CONVERSATION_TTL, CONVERSATION_STORE_FILE
# Proxy            PROXY, PROXY_POOL
```

---

## 🗂 Cấu trúc project

```
arena-web2api/
├── main.py                     ← FastAPI app + lifespan
├── requirements.txt
├── setup.sh
├── .env.example
├── README.md  ARCHITECTURE.md  CHANGELOG.md  CONTRIBUTING.md
├── src/
│   ├── config.py               ← toàn bộ cấu hình từ env
│   ├── logger.py               ← logging có màu
│   ├── errors.py               ← hệ thống lỗi phân cấp
│   ├── models.py               ← Pydantic schemas (OpenAI + Arena)
│   ├── tokenizer.py            ← tiktoken + heuristic
│   ├── utils.py                ← SSE chunk builder, backoff, fingerprint
│   ├── cookie_pool.py          ← pool cookie xoay vòng + health
│   ├── session.py              ← headers + proxy rotation
│   ├── model_registry.py       ← dynamic UUID sync (TTL)
│   ├── sse_parser.py           ← SSE wire decoder + Arena event parser
│   ├── conversation_store.py   ← persistence multi-turn
│   ├── conversation.py         ← multi-turn manager (incremental)
│   ├── rate_limiter.py         ← token bucket RPM/TPM
│   ├── circuit_breaker.py      ← CLOSED/OPEN/HALF_OPEN
│   ├── metrics.py              ← đếm request/token/latency
│   ├── client.py               ← Arena API client (retry, breaker, …)
│   └── routes/
│       ├── chat.py             ← /v1/chat/completions
│       ├── battle.py           ← /v1/battle, /v1/battle/vote
│       ├── models.py           ← /v1/models
│       └── admin.py            ← /health, /admin/*
└── tests/
    ├── test_pipeline.py        ← integration (chat/multi-turn/battle/stream/metrics)
    └── test_sse.py             ← SSE parser + backoff unit tests
```

---

## 🧪 Test

```bash
bash tests/run_tests.sh
# hoặc
python3 tests/test_sse.py
python3 tests/test_pipeline.py
```

---

## 🔧 Debug

```bash
DEBUG=true python3 main.py          # log chi tiết
curl localhost:8000/admin/status     # tổng quan
curl localhost:8000/admin/metrics    # lỗi/latency theo model
curl localhost:8000/admin/breaker    # circuit breaker
```

Khi lỗi 403 (cookie hết hạn):
```bash
curl -X POST localhost:8000/admin/cookies/validate   # health-check pool
# hoặc cập nhật .env rồi restart
```

| Cookie | Thời hạn |
|--------|---------|
| `cf_clearance` | 1–2 ngày |
| `arena-auth-prod-v1` | 1–2 tuần |

---

## 🗺 Roadmap

- [ ] Tool calling / function calling (khi Arena expose)
- [ ] Web search toggle qua payload
- [ ] WebSocket gateway
- [ ] OpenTelemetry tracing
- [ ] Vision chính thức (khi RE rõ attachment format)

---

## ⚠️ Lưu ý

- Mục đích nghiên cứu; tôn trọng ToS Arena.
- Cookie là thông tin cá nhân — **không commit `.env`**.
- Endpoint thật: `POST /nextjs-api/stream/create-evaluation`. Khi Arena đổi, xem `CONTRIBUTING.md`.

## License

MIT
