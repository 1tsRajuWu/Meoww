#!/usr/bin/env bash
# ==============================================================================
# Render & Uptime Health Checker
# Pings your live Render cloud deployment to verify it is awake and answering.
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RENDER_URL="https://meow-discord-bot.onrender.com"

# Check .env if available for DASHBOARD_BASE_URL
if [ -f .env ]; then
    ENV_URL=$( (grep -E "^DASHBOARD_BASE_URL=" .env || true) | cut -d '=' -f2- | tr -d '"' | tr -d "'")
    if [ -n "$ENV_URL" ] && [[ "$ENV_URL" == *"onrender.com"* ]]; then
        RENDER_URL="$ENV_URL"
    fi
fi

TARGET_URL="${1:-$RENDER_URL/health}"
echo "🌐 Testing cloud health check at: $TARGET_URL"

START_TIME=$(date +%s%N 2>/dev/null || date +%s)
HTTP_CODE=$(curl -s -o /tmp/dacat_health_resp.json -w "%{http_code}" --max-time 60 "$TARGET_URL" || echo "000")

if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ Success! HTTP 200 OK received."
    echo "📄 Response Summary:"
    if command -v python3 >/dev/null 2>&1; then
        python3 -m json.tool /tmp/dacat_health_resp.json 2>/dev/null || cat /tmp/dacat_health_resp.json
    else
        cat /tmp/dacat_health_resp.json
    fi
    echo ""
    echo "🟢 Render Web Service is AWAKE and answering 24/7."
elif [ "$HTTP_CODE" = "000" ]; then
    echo "❌ Connection timed out or failed to reach $TARGET_URL."
    echo "   If the free instance was sleeping, it may take 45-60s to boot up. Try again."
else
    echo "⚠️ Received HTTP $HTTP_CODE from $TARGET_URL."
    cat /tmp/dacat_health_resp.json 2>/dev/null || true
fi
rm -f /tmp/dacat_health_resp.json
