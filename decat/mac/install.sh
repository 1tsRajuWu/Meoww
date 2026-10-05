#!/usr/bin/env bash
# Install the Mac half of /decat: Ollama, the model, a config file, and a login
# item that starts answering when you log in and restarts itself if it dies.
#
# One command, run once, on the laptop that answers questions:
#
#     bash decat/mac/install.sh
#
# It is safe to run again: every step checks before it changes anything, and the
# model pull is a no-op once the model is there.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
HOME_DIR="${DECAT_HOME:-$HOME/.decat}"
MODEL="${DECAT_MODEL:-qwen3-vl:8b}"
LABEL="com.decat.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

say() { printf '\033[1m▸\033[0m %s\n' "$1"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this half is macOS-only (found $(uname -s))"
command -v python3 >/dev/null || fail "python3 not found — install Xcode command line tools first"

# ── Ollama ───────────────────────────────────────────────────────────────────
if command -v ollama >/dev/null; then
  say "Ollama is already installed ($(ollama --version 2>/dev/null | head -1))"
elif command -v brew >/dev/null; then
  say "Installing Ollama with Homebrew…"
  brew install ollama
else
  fail "neither ollama nor Homebrew is here — install Ollama from https://ollama.com/download, then run this again"
fi

# `brew services` is what makes it come back after a reboot; without it the model
# is only there for as long as a terminal stays open, which is exactly the kind of
# half-working state the offline line exists to stop being a mystery.
if command -v brew >/dev/null && brew list --formula ollama >/dev/null 2>&1; then
  say "Starting Ollama at login (brew services)…"
  brew services start ollama >/dev/null 2>&1 || true
else
  say "Start Ollama yourself: `ollama serve` (or add it to your login items)"
fi

# Wait for the daemon to answer before asking it for six gigabytes.
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
  || fail "Ollama is not answering on 127.0.0.1:11434 — start it and run this again"

say "Pulling $MODEL (6.1 GB the first time, nothing after that)…"
ollama pull "$MODEL"

# ── the config the agent reads ───────────────────────────────────────────────
mkdir -p "$HOME_DIR/history"
chmod 700 "$HOME_DIR"

if [ ! -f "$HOME_DIR/agent.env" ]; then
  say "Writing $HOME_DIR/agent.env — fill in the two values it asks for"
  cat > "$HOME_DIR/agent.env" <<'EOF'
# Decat — this machine's settings. Read by the login item, not by the repo.
#
# The credential the bot's host expects. It is the same value the Space has as
# ADMIN_PASSKEY, or — better — a DECAT_TOKEN you set in both places so a laptop
# that goes missing cannot open the rest of the operator surface.
DECAT_TOKEN=

# Where the bot lives.
DECAT_SPACE=https://1tsrajowo-meow.hf.space

# The model, and the fast one for short questions (blank = same as the big one).
DECAT_MODEL=qwen3-vl:8b
DECAT_FAST_MODEL=qwen3-vl:4b

# The only third party that ever sees anything: none | ddg | brave | searxng.
DECAT_SEARCH=ddg
DECAT_SEARCH_KEY=
DECAT_SEARXNG_URL=

# Where the history of `Public` questions is kept. `Private` questions are never
# written here — or anywhere else.
DECAT_HOME=
EOF
  chmod 600 "$HOME_DIR/agent.env"
else
  say "Keeping the existing $HOME_DIR/agent.env"
fi

# ── the `decat` command ──────────────────────────────────────────────────────
BIN_DIR="$HOME_DIR/bin"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/decat" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
[ -f "$HOME_DIR/agent.env" ] && . "$HOME_DIR/agent.env"
set +a
export PYTHONPATH="$REPO\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m decat.mac "\$@"
EOF
chmod 755 "$BIN_DIR/decat"
say "Installed $BIN_DIR/decat (add $BIN_DIR to your PATH to type just \`decat\`)"

# ── the login item ───────────────────────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN_DIR/decat</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StandardOutPath</key><string>$HOME_DIR/agent.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/agent.log</string>
</dict>
</plist>
EOF
launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
say "Login item installed — it starts at login and restarts if it stops"

# ── does it actually work ────────────────────────────────────────────────────
say "Checking this machine end to end…"
set +e
"$BIN_DIR/decat" doctor
DOCTOR=$?
set -e

cat <<EOF

Done. Two things left, both in Discord:

  1. The Space needs the same credential you put in $HOME_DIR/agent.env.
  2. The server needs the system switched on for it (an administrator, once).

The switch is at http://127.0.0.1:8787 while the agent is running, and
"$BIN_DIR/decat on|off" works from anywhere. The log is $HOME_DIR/agent.log.
EOF
[ "$DOCTOR" -eq 0 ] || printf '\033[33m!\033[0m doctor reported something above — fix that before asking a question.\n'
exit 0
