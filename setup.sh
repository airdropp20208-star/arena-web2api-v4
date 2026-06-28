#!/bin/bash
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   arena-web2api — Setup Script"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Cập nhật package list
echo "📦 Cập nhật apt..."
apt-get update -qq

# Cài Python nếu chưa có
if ! command -v python3 &>/dev/null; then
    echo "🐍 Cài Python3..."
    apt-get install -y python3 python3-pip -qq
fi

# Cài pip nếu chưa có
if ! command -v pip3 &>/dev/null; then
    apt-get install -y python3-pip -qq
fi

echo "📥 Cài dependencies..."
pip3 install -r requirements.txt --quiet

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
echo "Bước tiếp theo:"
echo "  1. Lấy cookie từ Kiwi Browser (xem README.md)"
echo "  2. nano .env  →  điền cookie"
echo "  3. python3 main.py"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
