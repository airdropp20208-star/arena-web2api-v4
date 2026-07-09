# Arena Token Broker — Kiwi Browser Extension

Extension Chrome (Manifest V2) cho Kiwi Browser trên Android. Sinh reCAPTCHA
Enterprise v3 token trong tab arena.ai, gửi về server arena-web2api qua WebSocket.

## Tại sao cần

Arena backend hard-enforce reCAPTCHA Enterprise v3. Token gen bằng headless
Chromium (Playwright) bị Google chấm điểm thấp → Arena reject với 403
"recaptcha validation failed".

Kiwi Browser là Chrome thật trên Android — có:
- Real Android user agent
- Real Android fingerprint (touch, sensors, WebGL)
- Real browser cookies/history
- Real mobile IP

→ Google reCAPTCHA score cao (0.7-0.9) → Arena accept token.

## Kiến trúc

```
┌────────────────────────────────────────────┐
│  Android Phone (Termux + Kiwi Browser)     │
│                                            │
│  ┌──────────────┐    ┌─────────────────┐  │
│  │ Termux       │    │ Kiwi Browser    │  │
│  │ arena-web2api│←──→│ + This Extension│  │
│  │ :8000 (HTTP) │    │ arena.ai tab    │  │
│  │ :8765 (WS)   │    │ (logged in)     │  │
│  └──────────────┘    └─────────────────┘  │
│         ↑                  ↑              │
│         │  WS localhost    │              │
│         └──────────────────┘              │
└────────────────────────────────────────────┘
```

Flow:
1. User gửi chat request → server `:8000`
2. Server cần reCAPTCHA token → gọi token broker
3. Token broker gửi WS message `{"type":"need_token","id":"..."}` tới extension
4. Extension executeScript trong arena.ai tab → `grecaptcha.enterprise.execute()`
5. Extension gửi token back qua WS
6. Server dùng token trong httpx request tới Arena
7. Server stream response về user

## Cài đặt

### Bước 1: Chuẩn bị files

Copy thư mục `extension/` vào ĐT (qua USB, Google Drive, hoặc git clone).

### Bước 2: Mở Kiwi Browser

1. Mở Kiwi Browser trên ĐT
2. Vào menu (⋮) → **Extensions** (hoặc gõ `kiwi://extensions` vào address bar)
3. Bật **Developer mode** (toggle ở góc trên phải)
4. Click **+ (from .zip/.crx/.user.js)** hoặc **Load unpacked**

### Bước 3: Load extension

**Cách A — Load unpacked (khuyên dùng):**
1. Click **Load unpacked**
2. Chọn thư mục `extension/` (chứa `manifest.json`)
3. Extension xuất hiện trong danh sách

**Cách B — Pack thành .zip trước:**
```bash
# Trên máy tính hoặc Termux
cd extension/
zip -r arena-token-broker.zip manifest.json background.js popup.html popup.js icons/
# Copy arena-token-broker.zip sang ĐT, mở trong Kiwi
```

### Bước 4: Login Arena

1. Mở tab mới trong Kiwi
2. Vào `https://arena.ai`
3. Login bằng email/password (đã có account)
4. **GIỮ TAB arena.ai MỞ** (không đóng, không switch tab quá lâu)

### Bước 5: Start server trên Termux

```bash
# Trong Termux
cd arena-web2api-v4
cp .env.example .env
# Edit .env: set ARENA_AUTH_COOKIE + CF_CLEARANCE (lấy từ Kiwi DevTools)
nano .env
bash run.sh
```

Server start xong sẽ log:
```
🔑 reCAPTCHA strategy: extension
🔌 Token broker: ws://127.0.0.1:8765 (extension connects here)
```

### Bước 6: Verify extension kết nối

1. Click icon extension trên Kiwi (góc trên phải)
2. Popup hiện ra:
   - **Status:** "✓ Connected to ws://localhost:8765"
   - **Arena tab:** "✓ open (1 tab)"
   - **Tokens generated:** 0
3. Click **Test Token** — extension sẽ gen 1 token test
   - Nếu thành công: "✓ Token generated successfully!"
   - Nếu fail: lỗi hiện trong popup

### Bước 7: Test end-to-end

```bash
# Trong Termux (hoặc curl từ máy khác)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "arena-battle",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

Hoặc kiểm tra broker status:
```bash
curl http://localhost:8000/admin/broker
# → {"strategy": "extension", "extension_connected": true, "token_count": 1, ...}

curl -X POST http://localhost:8000/admin/broker/test
# → {"ok": true, "token_length": 2084, "elapsed_ms": 1200, ...}
```

## Troubleshooting

### Extension không kết nối được server

**Triệu chứng:** Popup hiện "✗ Disconnected"

**Nguyên nhân & fix:**
1. **Server chưa start** → Chạy `bash run.sh` trong Termux
2. **Port sai** → Mặc định 8765. Trong popup, đổi WS URL cho khớp với `TOKEN_BROKER_PORT` trong `.env`
3. **Firewall chặn localhost** → Hiếm trên Android, nhưng thử tắt các app VPN/adblocker
4. **Kiwi throttle background** → Mở popup extension thường xuyên, hoặc keep Kiwi ở foreground

### Extension connected nhưng token fail

**Triệu chứng:** Test Token → "✗ grecaptcha not loaded after 10s"

**Fix:**
1. Mở tab `https://arena.ai` (đã login)
2. Refresh tab (Ctrl+R) — grecaptcha library cần load lại
3. Đợi 5s sau khi load xong
4. Test lại

### Token gen OK nhưng Arena vẫn 403

**Triệu chứng:** Server log "reCAPTCHA validation failed" dù extension gen được token

**Nguyên nhân:**
- Account Arena đã bị flag (do thử bypass nhiều lần trước)
- reCAPTCHA score vẫn thấp (do AI Arena detect unusual pattern)

**Fix:**
1. Tạo account Arena mới
2. Login trên Kiwi, đợi 5-10 phút trước khi dùng
3. Đừng gửi quá nhiều request liên tục — cách 5-10s giữa các request

### Token gen chậm (>5s)

**Nguyên nhân:** Kiwi throttle background tab

**Fix:**
1. Keep tab arena.ai active ( foreground)
2. Tắt battery optimization cho Kiwi
3. Dùng phone charger khi chạy agent liên tục

## Multi-session song song

Extension gen token theo yêu cầu — mỗi request chat = 1 token mới. Server
queue các request, extension xử lý tuần tự nhưng nhanh (~1-2s/token).

Test: gửi 3 request song song → 3 token unique trong ~3s.

```bash
# Test 3 request song song
for i in 1 2 3; do
  curl -X POST http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"arena-battle\",\"messages\":[{\"role\":\"user\",\"content\":\"reply $i\"}]}" &
done
wait
```

## Agent liên tục

Để agent chạy liên tục:
1. **Phone luôn awake**: Settings → Display → Screen timeout → 10 minutes (or Never)
2. **Kiwi luôn mở**: Don't swipe away Kiwi from recent apps
3. **Tab arena.ai luôn open**: Don't close
4. **Termux always-on**: Settings → Battery → Termux → Don't optimize
5. **Use `run.sh`**: auto-restart nếu server crash

## Running Kiwi in background (để phone có thể tắt màn hình)

Mặc định Android sẽ kill background apps sau ~5-10 phút để tiết kiệm pin.
Để extension + tab arena.ai sống sót khi phone tắt màn hình, cần làm các bước sau.

### Cách 1: Settings UI (không cần PC)

1. **Battery optimization**:
   - Settings → Apps → Kiwi Browser → Battery → **Unrestricted** (hoặc "No restrictions")
   - Settings → Apps → Termux → Battery → **Unrestricted**
2. **Stay awake while charging** (developer option):
   - Settings → About phone → tap **Build number** 7 lần → mở Developer options
   - Settings → System → Developer options → **Stay awake** = ON
3. **Screen timeout**:
   - Settings → Display → Screen timeout → 10 minutes (or Never if you don't care about battery)
4. **Don't close Kiwi**:
   - Lock app Kiwi trong recent apps (lock icon trên multi-task view)

### Cách 2: ADB (cần PC hoặc Termux:pkg install android-tools)

Nếu đã enable USB debugging:

```bash
# Whitelist Kiwi + Termux khỏi Doze (battery saver)
adb shell dumpsys deviceidle whitelist +com.kiwibrowser.browser
adb shell dumpsys deviceidle whitelist +com.termux

# Disable battery optimization cho Kiwi
adb shell cmd appops set com.kiwibrowser.browser RUN_IN_BACKGROUND allow
adb shell cmd appops set com.kiwibrowser.browser RUN_ANY_IN_BACKGROUND allow

# Stay awake while charging
adb shell settings put global stay_on_while_plugged_in 3

# (Optional) Force Kiwi foreground — không recommend vì cản việc dùng ĐT
# adb shell am start -n com.kiwibrowser.browser/com.android.chrome.Main
```

### Cách 3: Stay Alive app (Play Store)

Cài app như "Stay Alive! Keep screen awake" hoặc "Screen Alive" — keep screen on
cho app cụ thể. Đơn giản nhất nếu không muốn config.

### Verify running background

1. Tắt màn hình, đợi 10 phút
2. Mở lại, check popup extension → vẫn "✓ Connected" + token count tăng
3. Nếu fail → extension đã bị kill, cần check battery settings

## Auto-recovery mechanism (đã có sẵn)

Extension có 3 cơ chế auto-recovery:

1. **Auto-open arena.ai tab**:
   - Khi extension start (background load) → open tab nếu chưa có
   - Khi user đóng tab → reopen sau 2s
   - Alarm 24s check tab còn sống

2. **Auto-relogin** (cần save credentials trong popup):
   - Khi server báo 401 (arena-auth expired) → request relogin
   - Extension gọi `/nextjs-api/sign-in/email` trong tab arena.ai
   - Cookie mới tự cập nhật

3. **Auto-refresh cookies**:
   - Khi auth fail (non-reCAPTCHA) → server gọi `request_cookies()`
   - Extension extract cookies qua `chrome.cookies.get`
   - Server update cookie pool entry

Để auto-relogin hoạt động, **phải save email/password trong popup** (mục
"Arena credentials" — collapsible section).

## Backup/restore credentials — fix #26

Credentials (email/password) saved trong `chrome.storage.local`. Khi update
extension hoặc reinstall Kiwi, storage có thể bị clear.

### Backup

```javascript
// Mở popup extension → F12 (DevTools) → Console
// Chạy:
chrome.storage.local.get(null, (data) => {
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'arena-broker-backup.json';
    a.click();
});
```

Hoặc manual: copy-paste từ popup UI vào file text.

### Restore

Sau khi reinstall extension:

```javascript
// Mở popup → F12 → Console
// Đọc file backup, paste JSON vào:
const backup = {/* paste JSON here */};
chrome.storage.local.set(backup, () => {
    console.log('Restored:', Object.keys(backup));
    location.reload();
});
```

### Alternative: re-enter qua popup

Đơn giản nhất — mở popup, nhập lại email/password, click "Save Credentials".
Mất 30 giây.

## Ngữ cảnh (conversation context) — có mất không?

Server có 2 chế độ:

### Mặc định (RAM only) — KHÔNG recommended cho agent dài

```env
CONVERSATION_STORE_FILE=       # trống
CONVERSATION_TTL=1800          # 30 phút
```

→ Server restart hoặc 30 phút không dùng = mất conversation state.
Agent phải gửi lại full history (OpenAI client tự làm).

### Recommended cho agent dài

```env
CONVERSATION_STORE_FILE=./data/conversations.json  # persist
CONVERSATION_TTL=7200                              # 2 giờ
```

→ Server restart vẫn giữ state. Conversation sống 2 giờ sau last activity.
Atomic write (fix B9) — không corrupt nếu crash giữa chừng.

### Ngữ cảnh Arena

- Server cache `conversationId` của Arena → tái dùng cho multi-turn thật
- Khi expire → server auto tạo conversation mới, gửi history flattened
- Arena-side context do Arena manage, server không control

### Agent memory (ngoài server)

Nếu bạn dùng agent framework (AutoGPT, LangChain agent, v.v.):
- Server arena-web2api chỉ là proxy — không có agent memory
- Agent framework phải tự manage context (vector DB, conversation buffer, v.v.)
- Server nhận full history mỗi request từ agent

## Limitations

- **1 extension = 1 token at a time**: nếu 5 request tới cùng lúc, extension
  xử lý tuần tự. Có thể mất 5-10s cho 5 request.
- **Tab arena.ai phải active**: nếu đóng tab hoặc kill Kiwi → token fail.
- **Token expire ~120s**: nếu server chờ quá lâu giữa gen token và gửi request,
  token có thể expire. Server có retry mechanism.
- **Account flagging**: gửi quá nhiều request = flagged. Giữ RPS < 5 để an toàn.

## Files

```
extension/
├── manifest.json   ← Chrome MV2 manifest
├── background.js   ← Persistent background, WS client, token gen
├── popup.html      ← UI: status, config, test
├── popup.js        ← Popup logic
├── icons/
│   ├── icon-48.png
│   └── icon-128.png
└── README.md       ← This file
```

## License

MIT — same as arena-web2api.
