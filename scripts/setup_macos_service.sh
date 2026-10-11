#!/usr/bin/env bash
# ==============================================================================
# Helper to install or uninstall the macOS LaunchAgent 24/7 service.
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.dacat.bot.plist"
LOGS_DIR="$ROOT/logs"

mkdir -p "$LOGS_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

ACTION="${1:-install}"

case "$ACTION" in
    install|enable)
        echo "Installing macOS 24/7 LaunchAgent..."
        sed -e "s|/Users/raj/Documents/Meoww|$ROOT|g" "$ROOT/scripts/com.dacat.bot.plist" > "$TARGET_PLIST"
        
        # Unload existing if running
        launchctl unload "$TARGET_PLIST" 2>/dev/null || true
        # Load and start
        launchctl load -w "$TARGET_PLIST"
        echo "✅ DaCat bot is now registered as a macOS 24/7 background LaunchAgent!"
        echo "   It will automatically run at login/boot and restart if it crashes."
        echo "   Logs: $LOGS_DIR/launchd_stdout.log"
        ;;
    uninstall|disable)
        echo "Unloading macOS LaunchAgent..."
        launchctl unload "$TARGET_PLIST" 2>/dev/null || true
        rm -f "$TARGET_PLIST"
        echo "✅ DaCat bot LaunchAgent has been removed."
        ;;
    status)
        if launchctl list | grep -q "com.dacat.bot"; then
            echo "🟢 com.dacat.bot LaunchAgent is LOADED & ACTIVE in macOS launchd."
        else
            echo "🔴 com.dacat.bot LaunchAgent is NOT currently loaded."
        fi
        ;;
    *)
        echo "Usage: $0 {install|uninstall|status}"
        exit 1
        ;;
esac
