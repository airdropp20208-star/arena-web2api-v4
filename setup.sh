#!/bin/bash
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   arena-web2api v4 — Setup Script"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Phát hiện môi trường
if [ -d "/data/data/com.termux" ]; then
    echo "📱 Phát hiện Termux (Android)"
    IS_TERMUX=1
else
    IS_TERMUX=0
fi

# Cập nhật package list
if [ "$IS_TERMUX" = "1" ]; then
    echo "📦 Cập nhật Termux packages..."
    pkg update -y -qq
    # Cài build deps cho tiktoken
    pkg install -y python python-pip rust binutils clang make -qq
else
    if command -v apt-get &>/dev/null; then
        echo "📦 Cập nhật apt..."
        apt-get update -qq
    fi
    # Cài Python nếu chưa có
    if ! command -v python3 &>/dev/null; then
        echo "🐍 Cài Python3..."
        apt-get install -y python3 python3-pip -qq
    fi
fi

# Cài pip nếu chưa có
if ! command -v pip3 &>/dev/null; then
    if [ "$IS_TERMUX" = "1" ]; then
        pkg install -y python-pip -qq
    else
        apt-get install -y python3-pip -qq
    fi
fi

echo "📥 Cài dependencies..."
if [ "$IS_TERMUX" = "1" ]; then
    # Termux: cần --break-system-packages hoặc dùng venv
    pip3 install -r requirements.txt --quiet --break-system-packages 2>&1 || {
        echo "⚠  pip install fail — thử install từng package"
        for pkg in $(cat requirements.txt); do
            pip3 install "$pkg" --quiet --break-system-packages 2>&1 | tail -2
        done
    }
    # curl for keepalive.sh + test scripts — fix #28
    pkg install -y curl -qq 2>/dev/null || true
    # Optional: Termux:API cho keepalive (battery, sensors)
    pkg install -y termux-api -qq 2>/dev/null || true
    echo "ℹ  Để dùng keepalive.sh đầy đủ, cài Termux:API app từ F-Droid/Play Store"
else
    pip3 install -r requirements.txt --quiet
    # Ensure curl installed
    if ! command -v curl &>/dev/null; then
        apt-get install -y curl -qq
    fi
fi

# Tạo thư mục data
mkdir -p data

# Tạo .env nếu chưa có
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  File .env vừa được tạo từ .env.example"
    echo "   → Mở .env và điền cookie vào:"
    echo "   nano .env"
else
    echo "✅ .env đã tồn tại"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Setup hoàn tất!"
echo ""
echo "Cài 'arena' command globally:"
echo "  bash install.sh"
echo ""
echo "Sau đó dùng:"
echo "  arena setup    ← cấu hình .env interactive"
echo "  arena start    ← khởi động server + keepalive"
echo "  arena status   ← xem trạng thái"
echo "  arena logs     ← xem log real-time"
echo "  arena stop     ← dừng"
echo ""
echo "Hoặc chạy trực tiếp nếu chưa install:"
echo "  python3 main.py"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
