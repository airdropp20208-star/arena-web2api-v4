# Đóng góp / Bảo trì

Dự án reverse-engineer giao diện web của Arena.ai. Khi Arena thay đổi, cập nhật
theo các điểm dưới đây. Code đã modular nên mỗi thay đổi chỉ đụng 1 file.

## 1. Cập nhật Model UUID → tự động rồi

Không cần sửa tay. `model_registry.py` tự fetch `/nextjs-api/models`:
- Kiểm tra: `GET /v1/models/refresh` hoặc `GET /admin/registry`.
- Nếu registry fetch thất bại (403), cookie chết — fix cookie, không phải code.

Chỉ khi Arena đổi **endpoint** `/nextjs-api/models` mới cần sửa `ARENA_MODELS_URL`
trong `config.py`.

## 2. SSE format thay đổi → `sse_parser.py`

`parse_arena_event()` đã chịu nhiều shape (delta/content/text/message/finish_reason/
model_index/reveal/error). Nếu Arena thêm field mới:

1. DevTools → Network → `create-evaluation` → Response.
2. Thêm extraction trong `_interpret_chunk()`.
3. Thêm test trong `tests/test_sse.py`.

## 3. Multi-turn → `conversation.py`

Payload link turn qua `conversationId`. Nếu Arena dùng field khác (vd
`sessionId`, `parentMessageId`):
- Đổi trong `build_direct_payload()` / `build_battle_payload()` (`client.py`).
- Logic match giữ nguyên (fingerprint trong `utils.py`).

## 4. Endpoint chat đổi → `config.py`

`ARENA_STREAM_URL = .../nextjs-api/stream/create-evaluation`.

## 5. Vote endpoint → `client.py:submit_vote`

`ARENA_VOTE_URL` + payload `submit_vote`. Nếu Arena đổi field name vote, sửa 1 dòng.

## Quy trình phát triển

```bash
bash tests/run_tests.sh     # PHẢI pass trước khi commit
DEBUG=true python3 main.py  # chạy local
```

## Báo lỗi

Mở issue kèm:
- Log (`DEBUG=true`) hoặc `GET /admin/status`, `/admin/metrics`.
- Response từ Arena (DevTools Network).
- Model + mode (direct/battle).
