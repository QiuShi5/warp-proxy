#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════
# warp-proxy — Enhanced WARP Proxy with License Pool Manager
# ═══════════════════════════════════════════════════════════════════

echo "=== warp-proxy starting ==="

APP_MODE="${APP_MODE:-node}"

if [ "$APP_MODE" = "manager" ]; then
    echo "Starting in manager mode..."
    cd /app
    exec python -m uvicorn backend.cluster_app:app --host 0.0.0.0 --port 8000 --log-level info
fi

if [ "$APP_MODE" != "node" ]; then
    echo "ERROR: Unsupported APP_MODE: $APP_MODE"
    exit 1
fi

# ── 1. Start dbus (required by warp-svc for D-Bus IPC) ─────────
echo "[1/7] Starting dbus..."
/etc/init.d/dbus start
sleep 2

# ── 2. Start WARP service ───────────────────────────────────────
echo "[2/7] Starting warp-svc..."
warp-svc &
WARP_SVC_PID=$!
sleep 4

# ── 3. Initial WARP registration ────────────────────────────────
echo "[3/7] Initial WARP registration..."
# Ignore error if already registered (e.g., volume restored)
warp-cli --accept-tos registration new 2>/dev/null || true

# Set proxy mode (SOCKS5 on 127.0.0.1:40000)
if ! warp-cli --accept-tos mode proxy; then
    echo "WARNING: Failed to set WARP proxy mode; continuing so the web UI can recover."
fi

# ── 4. Connect to WARP network ──────────────────────────────────
echo "[4/7] Connecting to WARP..."
if ! warp-cli --accept-tos connect; then
    echo "WARNING: Initial WARP connect failed; continuing so the web UI can recover."
fi

echo "Waiting for WARP to connect..."
for i in $(seq 1 15); do
    STATUS=$(warp-cli --accept-tos status 2>/dev/null || echo "waiting")
    STATUS_LOWER=$(printf "%s" "$STATUS" | tr '[:upper:]' '[:lower:]')
    if echo "$STATUS_LOWER" | grep -q "connected" && ! echo "$STATUS_LOWER" | grep -q "disconnected"; then
        echo "WARP connected successfully!"
        break
    fi
    sleep 2
done

# ── 5. Optional original-style WARP reconnect refresh ───────────
if [[ "${REFRESH_INTERVAL:-}" =~ ^[1-9][0-9]*$ ]]; then
    echo "WARP IP refresh enabled, interval: ${REFRESH_INTERVAL} minutes."
    (
        while true; do
            sleep $(( REFRESH_INTERVAL * 60 ))
            echo "$(date '+%Y-%m-%d %H:%M:%S') - Refreshing WARP connection..."
            warp-cli --accept-tos disconnect || true
            sleep 3
            warp-cli --accept-tos connect || true
        done
    ) &
else
    echo "WARP IP refresh disabled (REFRESH_INTERVAL is 0 or not set)."
fi

# Check initial IP
CURRENT_IP=$(curl --socks5 127.0.0.1:40000 --max-time 8 -s https://ifconfig.me 2>/dev/null || echo "unknown")
echo "Initial WARP IP: $CURRENT_IP"

# ── 6. Start Python backend (web UI + WARP manager) ────────────
echo "[6/7] Starting web management backend..."
cd /app
nohup python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 --log-level info \
    > /data/backend.log 2>&1 &
BACKEND_PID=$!
echo "Backend started (PID: $BACKEND_PID) on port 8000"
sleep 2

# ── 7. Build GOST auth string and start proxy forwarding ────────
echo "[7/7] Starting GOST proxy forwarding..."

AUTH_STRING=""
if [ -n "${PROXY_USER:-}" ] && [ -n "${PROXY_PASS:-}" ]; then
    AUTH_STRING="${PROXY_USER}:${PROXY_PASS}@"
    echo "Proxy credentials configured for user: $PROXY_USER"
else
    echo "WARNING: No proxy credentials set. Access is open (use only in trusted networks)."
fi

echo "=== warp-proxy ready ==="
echo "  Web UI:  http://0.0.0.0:8000"
echo "  SOCKS5:  :1080"
echo "  HTTP:    :8080"
echo "  WARP IP: $CURRENT_IP"
echo "================================="

# Start GOST in foreground (keeps container alive)
exec gost -L "http://${AUTH_STRING}:8080" -L "socks5://${AUTH_STRING}:1080" -F "socks5://127.0.0.1:40000"
