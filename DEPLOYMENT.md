# Deployment — Reverse proxy, HTTPS, migrations, alerting

## Reverse proxy (Caddy) — recommended for HTTPS

arena-web2api default bind 127.0.0.1:8000 (localhost only). Để expose public
với HTTPS, dùng Caddy reverse proxy.

### Cài Caddy

```bash
# Termux
pkg install caddy

# Linux
apt install caddy
```

### Caddyfile

```caddyfile
# /etc/caddy/Caddyfile hoặc ~/Caddyfile

api.yourdomain.com {
    reverse_proxy 127.0.0.1:8000

    # Forward WebSocket cho token broker (nếu expose broker)
    # Lưu ý: KHÔNG expose broker public — chỉ expose API endpoint
}

# Hoặc dùng subpath
yourdomain.com/arena/* {
    reverse_proxy 127.0.0.1:8000
}
```

### Start Caddy

```bash
caddy run --config ~/Caddyfile
# Hoặc daemon
caddy start --config ~/Caddyfile
```

Caddy tự động:
- Cấp SSL cert từ Let's Encrypt
- Renew cert trước khi expire
- Redirect HTTP → HTTPS

### Set API keys

```env
# .env
API_KEY_ENABLED=true
API_KEYS=yourkey1,yourkey2
ADMIN_TOKEN=your_admin_token
HOST=127.0.0.1  # still bind localhost, Caddy proxy handles public
```

Client gọi:
```bash
curl https://api.yourdomain.com/v1/chat/completions \
  -H "Authorization: Bearer yourkey1" \
  -H "Content-Type: application/json" \
  -d '{"model":"arena-battle","messages":[...]}'
```

## Reverse proxy (nginx) — alternative

```nginx
server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE/streaming support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;

        # WebSocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Cert với certbot:
```bash
certbot --nginx -d api.yourdomain.com
```

## Alerting webhook (Discord/Telegram) — fix #33

`keepalive.sh` hỗ trợ send notification khi server down/restart.

### Setup Discord webhook

1. Discord server → channel settings → Integrations → Webhooks → New Webhook
2. Copy webhook URL: `https://discord.com/api/webhooks/.../...`
3. Set env trong Termux:
   ```bash
   echo 'export ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/.../..."' >> ~/.bashrc
   source ~/.bashrc
   ```
4. Restart keepalive.sh

### Setup Telegram bot

1. Tạo bot via @BotFather → get token
2. Get chat ID: send message to bot, visit `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Set env:
   ```bash
   export ALERT_WEBHOOK_URL="https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>"
   ```

### Alert triggers

keepalive.sh sends alert khi:
- Server down (curl /health fail)
- Server restart (exit code != 0)
- Kiwi relaunch (process not found)
- Extension disconnect quá 5 phút
- Disk space low (<50MB)
- Battery low (<15%)

## Versioned migrations — fix #35

Khi schema conversation store thay đổi, dùng migration script.

### Migration script location

```
migrations/
├── 001_initial.py         ← Initial schema (v4.0.0)
├── 002_add_last_activity.py  ← v4.1.0: add last_activity field
└── README.md
```

### Run migrations

```bash
python3 migrate.py
# → Running migration 001_initial... OK
# → Running migration 002_add_last_activity... OK
# → All migrations applied
```

### Migration script template

```python
# migrations/001_initial.py
"""Initial schema — v4.0.0"""
VERSION = "4.0.0"

def migrate(data: list[dict]) -> list[dict]:
    """Transform conversation data to current schema."""
    # No-op for initial
    return data
```

```python
# migrations/002_add_last_activity.py
"""v4.1.0: add last_activity field for LRU eviction"""
VERSION = "4.1.0"

def migrate(data: list[dict]) -> list[dict]:
    for conv in data:
        if "last_activity" not in conv:
            conv["last_activity"] = conv.get("updated_at", 0)
    return data
```

### migrate.py runner

```python
#!/usr/bin/env python3
"""Run all pending migrations on conversation store."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
from src.config import CONVERSATION_STORE_FILE

def main():
    if not CONVERSATION_STORE_FILE or not os.path.exists(CONVERSATION_STORE_FILE):
        print("No conversation store to migrate")
        return 0

    # Load current data
    with open(CONVERSATION_STORE_FILE) as f:
        data = json.load(f)

    # Run migrations
    migrations_dir = Path("migrations")
    if not migrations_dir.exists():
        print("No migrations dir")
        return 0

    migration_files = sorted(migrations_dir.glob("*.py"))
    migration_files = [f for f in migration_files if f.name != "__init__.py"]

    for mf in migration_files:
        print(f"Running migration {mf.name}...")
        # Dynamic import
        import importlib.util
        spec = importlib.util.spec_from_file_location(mf.stem, mf)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "migrate"):
            data = mod.migrate(data)
        print(f"  ✓ {mf.name} OK")

    # Backup original, write migrated
    backup = CONVERSATION_STORE_FILE + ".pre-migration.bak"
    os.replace(CONVERSATION_STORE_FILE, backup)
    with open(CONVERSATION_STORE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"\n✓ All migrations applied. Backup: {backup}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

## HTTPS cho extension WS

Nếu expose server public với HTTPS, extension WS cần `wss://` thay vì `ws://`.

Update extension popup → WS URL:
```
wss://api.yourdomain.com/broker
```

Server cần route WS `/broker` → token broker. Hiện tại broker listen port riêng
(8765), cần thay đổi để integrate vào FastAPI app.

(Not implemented yet — currently broker runs on separate port. For production
multi-user, would need WS reverse proxy through Caddy/nginx.)

## Production checklist

- [ ] HOST=127.0.0.1 (default, secure)
- [ ] API_KEYS set (multiple keys for rotation)
- [ ] ADMIN_TOKEN set
- [ ] CORS_ORIGINS restrict to your domains
- [ ] DEBUG=false
- [ ] CONVERSATION_STORE_FILE set (persistence)
- [ ] CONVERSATION_TTL=7200+ (long enough for agent)
- [ ] COOKIE_AUTO_REFRESH=true (health check)
- [ ] RECAPTCHA_SOLVER=extension (or 2captcha)
- [ ] Caddy/nginx reverse proxy với HTTPS
- [ ] Alert webhook (Discord/Telegram)
- [ ] Backup script cho conversation store + cookies
- [ ] Monitoring: Prometheus scrape /metrics
- [ ] Log rotation configured
