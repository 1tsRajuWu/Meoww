#!/usr/bin/env bash
# ==============================================================================
# DaCat 24/7 Background Watchdog Runner
# 
# Usage:
#   ./scripts/run_247.sh start    # Start 24/7 watchdog daemon in background
#   ./scripts/run_247.sh stop     # Gracefully stop the 24/7 daemon
#   ./scripts/run_247.sh status   # Check daemon & bot process health
#   ./scripts/run_247.sh restart  # Restart the daemon
#   ./scripts/run_247.sh logs     # Follow live output logs
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOGS_DIR="$ROOT/logs"
PID_FILE="$ROOT/dacat_daemon.pid"
BOT_PID_FILE="$ROOT/dacat_bot.pid"
LOG_FILE="$LOGS_DIR/dacat_247.log"

mkdir -p "$LOGS_DIR"

# Pick Python interpreter (.venv preferred)
if [ -x "$ROOT/.venv/bin/python3" ]; then
    PYTHON_BIN="$ROOT/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "ERROR: python3 not found." >&2
    exit 1
fi

is_running() {
    local pid="$1"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

watchdog_loop() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] 🚀 DaCat 24/7 Watchdog initialized. Starting bot process..." >> "$LOG_FILE"
    
    while true; do
        if [ ! -f .env ]; then
            echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] ⚠️ Warning: .env file missing. Waiting 10s..." >> "$LOG_FILE"
            sleep 10
            continue
        fi

        echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] 🐾 Launching bot: $PYTHON_BIN bot.py" >> "$LOG_FILE"
        "$PYTHON_BIN" bot.py >> "$LOG_FILE" 2>&1 &
        BOT_PID=$!
        echo "$BOT_PID" > "$BOT_PID_FILE"

        wait "$BOT_PID" || true
        EXIT_CODE=$?
        rm -f "$BOT_PID_FILE"

        echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] ⚠️ Bot process exited with code $EXIT_CODE. Auto-restarting in 5 seconds..." >> "$LOG_FILE"
        sleep 5
    done
}

cmd_start() {
    if [ -f "$PID_FILE" ]; then
        PID="$(cat "$PID_FILE" 2>/dev/null || true)"
        if is_running "$PID"; then
            echo "DaCat 24/7 daemon is ALREADY running (Watchdog PID: $PID)."
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    echo "Starting DaCat 24/7 Watchdog Daemon in background..."
    nohup bash -c "$(declare -f watchdog_loop); ROOT='$ROOT' PYTHON_BIN='$PYTHON_BIN' LOG_FILE='$LOG_FILE' BOT_PID_FILE='$BOT_PID_FILE' watchdog_loop" >/dev/null 2>&1 &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$PID_FILE"

    sleep 1
    if is_running "$DAEMON_PID"; then
        echo "✅ DaCat 24/7 daemon successfully started (PID: $DAEMON_PID)."
        echo "   Logs: $LOG_FILE"
        echo "   Use './scripts/run_247.sh logs' to monitor output."
    else
        echo "❌ Failed to start DaCat 24/7 daemon. Check $LOG_FILE for details."
        exit 1
    fi
}

cmd_stop() {
    local stopped=false
    if [ -f "$BOT_PID_FILE" ]; then
        BOT_PID="$(cat "$BOT_PID_FILE" 2>/dev/null || true)"
        if is_running "$BOT_PID"; then
            echo "Stopping bot process (PID: $BOT_PID)..."
            kill "$BOT_PID" 2>/dev/null || true
            sleep 1
            kill -9 "$BOT_PID" 2>/dev/null || true
            stopped=true
        fi
        rm -f "$BOT_PID_FILE"
    fi

    if [ -f "$PID_FILE" ]; then
        PID="$(cat "$PID_FILE" 2>/dev/null || true)"
        if is_running "$PID"; then
            echo "Stopping 24/7 watchdog daemon (PID: $PID)..."
            kill "$PID" 2>/dev/null || true
            sleep 1
            kill -9 "$PID" 2>/dev/null || true
            stopped=true
        fi
        rm -f "$PID_FILE"
    fi

    if [ "$stopped" = true ]; then
        echo "✅ DaCat 24/7 daemon and bot process stopped."
    else
        echo "DaCat daemon is not currently running."
    fi
}

cmd_status() {
    local daemon_alive=false
    local bot_alive=false

    if [ -f "$PID_FILE" ]; then
        PID="$(cat "$PID_FILE" 2>/dev/null || true)"
        if is_running "$PID"; then
            daemon_alive=true
        fi
    fi

    if [ -f "$BOT_PID_FILE" ]; then
        BOT_PID="$(cat "$BOT_PID_FILE" 2>/dev/null || true)"
        if is_running "$BOT_PID"; then
            bot_alive=true
        fi
    fi

    echo "================ DaCat 24/7 Service Status ================"
    if [ "$daemon_alive" = true ]; then
        echo "🟢 Watchdog Daemon : RUNNING (PID: $(cat "$PID_FILE"))"
    else
        echo "🔴 Watchdog Daemon : STOPPED"
    fi

    if [ "$bot_alive" = true ]; then
        echo "🟢 Bot Process     : RUNNING (PID: $(cat "$BOT_PID_FILE"))"
    else
        echo "🔴 Bot Process     : STOPPED"
    fi
    echo "📄 Log file        : $LOG_FILE"
    echo "=========================================================="
}

cmd_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -n 50 -f "$LOG_FILE"
    else
        echo "No logs found yet at $LOG_FILE."
    fi
}

ACTION="${1:-status}"
case "$ACTION" in
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_stop
        sleep 2
        cmd_start
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
