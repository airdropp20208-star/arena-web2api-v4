# Chạy ngầm hoàn toàn — Hướng dẫn xem TikTok + chơi game nhẹ

**Không cần ADB.** Tất cả config làm qua Settings UI trên ĐT.

Mục tiêu: Server arena-web2api + extension chạy 24/7 trên ĐT, bạn vẫn xem TikTok
và chơi game nhẹ bình thường, không bị gián đoạn.

## Setup 1 lần (5 phút, không cần PC/ADB)

### Bước 1: Battery optimization off — quan trọng nhất

```
Settings → Apps → Kiwi Browser → Battery → Unrestricted
Settings → Apps → Termux → Battery → Unrestricted
```

**Lý do:** Android kill background apps để tiết kiệm pin. Unrestricted = app
chạy tự do, không bị kill.

### Bước 2: Stay awake khi sạc (developer option)

```
Settings → About phone → tap "Build number" 7 lần (mở Developer options)
Settings → System → Developer options → Stay awake = ON
```

**Lý do:** Phone không sleep khi đang sạc → server không bị suspend.

### Bước 3: Termux keep foreground service

Trong Termux:
```bash
termux-wake-lock
```

**Lý do:** Acquire wakelock — phone không sleep khi Termux chạy. Để release:
```bash
termux-wake-unlock
```

### Bước 4: Lock apps trong recent (chống swipe-kill)

- Mở multi-task view (vuốt từ đáy lên, hoặc square button)
- Long-press Kiwi Browser card → lock (icon ổ khóa hiện ra)
- Long-press Termux card → lock

**Lý do:** Locked apps không bị kill khi bạn swipe clear all.

### Bước 5 (tùy chọn, cho phone restart): Cài Termux:Boot

Termux:Boot app auto-run script khi phone restart.

1. Tải **Termux:Boot** từ F-Droid:
   https://f-droid.org/packages/com.termux.boot/
   (KHÔNG có trên Play Store)
2. Mở Termux:Boot 1 lần (Android cần grant permission)
3. Copy boot script:
   ```bash
   mkdir -p ~/.termux/boot
   cp ~/arena-web2api-v4/termux-boot/start.sh ~/.termux/boot/arena-web2api
   chmod +x ~/.termux/boot/arena-web2api
   # Edit path nếu arena-web2api-v4 không ở $HOME:
   nano ~/.termux/boot/arena-web2api
   ```

**Sau khi phone restart:** Termux:Boot tự chạy script → `termux-wake-lock` + `keepalive.sh`.

## Khởi động hàng ngày (30 giây)

### Khởi động server + keepalive

```bash
# Trong Termux
cd ~/arena-web2api-v4
termux-wake-lock

# Start keepalive (nó sẽ start server tự động)
nohup bash keepalive.sh > /tmp/keepalive.log 2>&1 &
disown

# Verify
sleep 5
curl http://localhost:8000/health
# → {"status":"ok",...}

tail -20 /tmp/keepalive.log
# → "🚀 keepalive.sh starting..."
```

### Khởi động extension

1. Mở Kiwi Browser
2. Extension tự động kết nối (background persistent)
3. Tab arena.ai tự mở (extension auto-open)
4. Click icon extension → popup hiện "✓ Connected"

### Verify mọi thứ running

```bash
curl http://localhost:8000/admin/health-deep | python3 -m json.tool
```

Expected:
```json
{
  "status": "ok",
  "checks": [
    {"check": "arena.ai_reachable", "ok": true, ...},
    {"check": "recaptcha_site_key_valid", "ok": true, ...},
    {"check": "stream_endpoint_exists", "ok": true, ...},
    {"check": "extension_connected", "ok": true, ...},
    {"check": "cookie_pool", "ok": true, ...}
  ]
}
```

## Xem TikTok + chơi game nhẹ

Sau khi setup xong, bạn có thể:
- Tắt màn hình ĐT
- Mở TikTok, xem video
- Mở game nhẹ (casual, idle, puzzle, card games)
- Server vẫn chạy ngầm, extension vẫn connected
- Agent vẫn nhận request qua `http://localhost:8000/v1/chat/completions`

### Game nhẹ (OK)

- Puzzle games (Candy Crush, 2048, sudoku)
- Idle games (AFK Arena, Idle Heroes)
- Card games (Yu-Gi-Oh, Hearthstone)
- Visual novels, story games
- 2D platformers nhẹ

RAM dùng <1GB → Android không kill background apps.

### Game vừa-nặng (thử nghiệm)

- Mobile Legends, Free Fire (low settings)
- PUBG Mobile (low settings)
- Among Us

RAM dùng 1-2GB → có thể kill Kiwi extension background, nhưng server (Termux) ổn.

**Nếu extension disconnect:** Mở Kiwi 1s → tab arena.ai auto-reopen → reconnect.

### Game nặng (KHÔNG recommend)

- Genshin Impact, Honkai Star Rail
- Call of Duty Mobile (high settings)
- Diablo Immortal

RAM dùng 3-4GB → Android sẽ kill cả Kiwi lẫn Termux. Cần 2 ĐT hoặc accept downtime.

## Monitor (tùy chọn)

### Xem log real-time

```bash
# Termux
tail -f /tmp/keepalive.log
# Hoặc server log
tail -f /tmp/arena-server.log
```

### Check health mỗi 10 phút

```bash
watch -n 600 'curl -s http://localhost:8000/admin/health-deep | python3 -m json.tool | grep -E "status|ok"'
```

### Notifications qua Termux:API (cài app Termux:API từ F-Droid)

```bash
# Test notify
termux-notification --title "Arena" --content "Server OK"

# Custom script: notify khi server down
cat > /tmp/check.sh << 'EOF'
#!/bin/bash
if ! curl -s -m 5 http://localhost:8000/health | grep -q "ok"; then
    termux-notification --title "⚠ Arena Down" --content "Server không respond"
fi
EOF
chmod +x /tmp/check.sh
# Add to crontab: */10 * * * * /tmp/check.sh
```

## Dừng server

```bash
pkill -f keepalive.sh
pkill -f "python3 main.py"
termux-wake-unlock
```

## Troubleshooting

### "Extension disconnected" sau khi xem TikTok 30 phút

**Nguyên nhân:** Android kill Kiwi background vì TikTok cần RAM.

**Fix:**
1. Mở Kiwi lại → extension tự reconnect (5-10s)
2. Tab arena.ai auto-reopen

### Server restart liên tục (keepalive log show nhiều restart)

**Nguyên nhân:** Termux bị kill → `python3 main.py` cũng chết → keepalive.sh
phát hiện và restart.

**Fix:** Verify `termux-wake-lock` đã acquire:
```bash
# Check if wakelock held
cat /proc/wakelocks 2>/dev/null | grep termux || termux-wake-lock
```

### Token gen fail (extension connected nhưng token rác)

**Nguyên nhân:** Tab arena.ai bị discard, grecaptcha library bị unload.

**Fix:** Mở tab arena.ai, refresh (Ctrl+R), đợi 5s. Extension sẽ tự reconnect.

### Cookie expired giữa khi dùng

**Auto-fix:** Server tự gọi `refresh_from_extension()` khi auth fail. Extension
extract cookies mới từ arena.ai (đã auto-relogin nếu có saved credentials).
Không cần can thiệp.

Nếu vẫn fail: mở popup extension → "Save Credentials" → "Relogin Now".

### Phone restart → mọi thứ tắt

**Fix:** Cài Termux:Boot (xem Bước 5 ở trên). Sau restart, script tự chạy.

Nếu chưa cài Termux:Boot, manual:
```bash
cd ~/arena-web2api-v4
termux-wake-lock
nohup bash keepalive.sh > /tmp/keepalive.log 2>&1 &
disown
# Mở Kiwi Browser
```

## Bảng tóm tắt

| Tình huống | Tác động | Recovery |
|---|---|---|
| ĐT tắt màn hình | None (wakelock) | Auto |
| Xem TikTok | None | Auto |
| Game nhẹ (puzzle, idle) | None | Auto |
| Game vừa (Mobile Legends low) | Có thể kill Kiwi ext | Mở Kiwi 5s |
| Game nặng (Genshin) | Kill cả Kiwi + Termux | Manual restart |
| Phone restart | All down | Termux:Boot auto |
| arena-auth expired | None | Auto refresh |
| cf_clearance expired | None | Auto refresh |
| Tab arena.ai đóng | Token gen fail 2s | Auto reopen |
| Kiwi Browser killed | Token gen fail | Mở Kiwi manual |
| Termux killed | Server down | keepalive restart (5-10s) |
| Network mất | 5xx errors | Auto retry khi có mạng |

## ADB có cần không?

**KHÔNG.** Tất cả steps trên làm qua Settings UI.

ADB chỉ tiện hơn (1 lệnh thay vì 4-5 clicks). Nếu bạn đã có ADB setup và
muốn dùng, các lệnh tương đương:

```bash
# Bước 1 tương đương (battery unrestricted)
adb shell dumpsys deviceidle whitelist +com.kiwibrowser.browser
adb shell dumpsys deviceidle whitelist +com.termux
adb shell cmd appops set com.kiwibrowser.browser RUN_ANY_IN_BACKGROUND allow
adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow

# Bước 2 tương đương (stay awake)
adb shell settings put global stay_on_while_plugged_in 3
```

Lệnh ADB persist qua reboot — chạy 1 lần là đủ, không cần ADB running 24/7.

## Tài liệu liên quan

- `extension/README.md` — hướng dẫn cài extension trên Kiwi
- `keepalive.sh` — script keepalive (xem comment trong file)
- `termux-boot/start.sh` — script boot khi phone restart
- `precheck.py` — pre-start checks (port, deps, .env)
