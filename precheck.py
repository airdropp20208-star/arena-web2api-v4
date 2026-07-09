#!/usr/bin/env python3
"""
Pre-start checks: verify port is available, curl installed, dependencies present.
Run before main.py to fail fast with helpful error messages.
"""
import os
import socket
import sys
import shutil


def check_port_available(host: str, port: int) -> bool:
    """Return True if port is available (nothing listening)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def main():
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))

    errors = []
    warnings = []

    # 1. Port check
    if not check_port_available(host, port):
        errors.append(
            f"Port {port} already in use. Either:\n"
            f"  - Stop the other process: pkill -f 'python3 main.py'\n"
            f"  - Or change PORT in .env: PORT={port + 1}"
        )

    # 2. curl check (used by keepalive.sh)
    if not shutil.which("curl"):
        warnings.append(
            "curl not found. Install: pkg install curl (Termux) or apt install curl (Linux).\n"
            "keepalive.sh and test scripts need curl."
        )

    # 3. Python deps check
    missing_deps = []
    for dep in ("fastapi", "uvicorn", "httpx", "pydantic", "dotenv", "websockets"):
        try:
            __import__(dep)
        except ImportError:
            missing_deps.append(dep)
    if missing_deps:
        errors.append(
            f"Missing Python deps: {', '.join(missing_deps)}\n"
            f"Install: pip3 install -r requirements.txt"
        )

    # 4. tiktoken optional check
    try:
        __import__("tiktoken")
    except ImportError:
        warnings.append(
            "tiktoken not installed — token counting will use heuristic fallback.\n"
            "Install: pip3 install tiktoken (needs Rust on Termux: pkg install rust)"
        )

    # 5. .env file check
    if not os.path.exists(".env"):
        warnings.append(
            ".env file not found. Run: cp .env.example .env && nano .env"
        )

    # 6. Cookie check (just warn, don't fail)
    if not os.getenv("ARENA_AUTH_COOKIE") and not os.getenv("COOKIE_POOL"):
        warnings.append(
            "ARENA_AUTH_COOKIE not set — server will start but all chat requests will fail.\n"
            "Set cookie in .env, or use extension 'Test Cookies' to auto-extract."
        )

    # Print warnings
    for w in warnings:
        print(f"⚠️  WARNING: {w}", file=sys.stderr)
        print(file=sys.stderr)

    # Print errors and exit
    if errors:
        for e in errors:
            print(f"❌ ERROR: {e}", file=sys.stderr)
            print(file=sys.stderr)
        sys.exit(1)

    print("✓ Pre-start checks passed")
    if warnings:
        print(f"  ({len(warnings)} warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
