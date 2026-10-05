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
# ── which model this machine can actually hold ──────────────────────────────
#
# Both models answer the same questions from the same sources; the difference is
# 6.1 GB of weights plus a KV cache against 3.3 GB. On a desktop that is a free
# upgrade. On a 16 GB fanless Air it is the difference between an answer and the
# machine paging its own memory out — measured on the Air this was written for:
# ~1.9 GB paged out during one answer, with the 8b resident. So the installer
# picks by the RAM it finds instead of assuming the better model fits, and it
# only downloads the one it picked: a 6 GB model nobody will load is not a
# harmless extra, it is a thing a machine in a hurry will reach for later.
#
# `DECAT_MODEL` still wins when it is set — this is a default, not a gate.
BIG_MODEL="qwen3-vl:8b"
SMALL_MODEL="qwen3-vl:4b"
RAM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
RAM_GB=$(( RAM_BYTES / 1073741824 ))
if [ -n "${DECAT_MODEL:-}" ]; then
  MODEL="$DECAT_MODEL"
  say "Using DECAT_MODEL=$MODEL as asked"
elif [ "$RAM_GB" -ge 24 ]; then
  MODEL="$BIG_MODEL"
  say "$RAM_GB GB of memory — $MODEL fits comfortably"
else
  MODEL="$SMALL_MODEL"
  say "$RAM_GB GB of memory — using $MODEL (the 8b needs about 6.1 GB resident; set DECAT_MODEL to override)"
fi

# The second model, only where there is room for both. On a machine that took the
# small one there is nothing to add: `Fast` reads two pages instead of three and
# skips the re-check, which is most of the time saved anyway.
FAST_MODEL="${DECAT_FAST_MODEL:-}"
if [ -z "$FAST_MODEL" ] && [ "$MODEL" = "$BIG_MODEL" ]; then
  FAST_MODEL="$SMALL_MODEL"
fi
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

say "Pulling $MODEL (a few GB the first time, nothing after that)…"
ollama pull "$MODEL"

# The small one, for `Fast`. Optional in the sense that `/decat` works without it
# once you delete the DECAT_FAST_MODEL line from agent.env — not optional in the
# sense that asking for Fast on a machine without it is an error.
if [ -n "$FAST_MODEL" ] && [ "$FAST_MODEL" != "$MODEL" ]; then
  say "Pulling $FAST_MODEL as well (3.3 GB) — this is what 'Fast' answers with"
  ollama pull "$FAST_MODEL" || say "Could not pull $FAST_MODEL — 'Fast' will not work until you do"
fi

# ── one model in memory at a time ────────────────────────────────────────────
#
# This is Ollama's setting, not the agent's: the weights live in the daemon, so
# the variable has to reach the service that started it. Homebrew's plist is the
# one place that survives a reboot, it is already carrying this machine's
# OLLAMA_FLASH_ATTENTION / OLLAMA_KV_CACHE_TYPE lines, so the same dictionary gets
# the cap. Backed up first, best-effort after that: a machine whose Ollama was
# started some other way still works, it is just less protected.
OLLAMA_PLIST="$HOME/Library/LaunchAgents/sh.brew.ollama.plist"
if [ -f "$OLLAMA_PLIST" ] && [ -x /usr/libexec/PlistBuddy ]; then
  if /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:OLLAMA_MAX_LOADED_MODELS" "$OLLAMA_PLIST" >/dev/null 2>&1; then
    say "Ollama already loads one model at a time"
  else
    cp "$OLLAMA_PLIST" "$HOME_DIR/ollama-plist.bak"
    if /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_MAX_LOADED_MODELS string 1" "$OLLAMA_PLIST" >/dev/null 2>&1; then
      say "Ollama will load one model at a time (old plist kept at $HOME_DIR/ollama-plist.bak)"
      brew services restart ollama >/dev/null 2>&1 || true
      for _ in $(seq 1 30); do
        curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
        sleep 1
      done
    fi
  fi
fi

# ── the config the agent reads ───────────────────────────────────────────────
mkdir -p "$HOME_DIR/history"
chmod 700 "$HOME_DIR"

if [ ! -f "$HOME_DIR/agent.env" ]; then
  say "Writing $HOME_DIR/agent.env — fill in the two values it asks for"
  cat > "$HOME_DIR/agent.env" <<EOF
# Decat — this machine's settings. Read by the login item, not by the repo.
#
# The credential the bot's host expects. It is the same value the Space has as
# ADMIN_PASSKEY, or — better — a DECAT_TOKEN you set in both places so a laptop
# that goes missing cannot open the rest of the operator surface.
DECAT_TOKEN=

# Where the bot lives.
DECAT_SPACE=https://1tsrajowo-meow.hf.space

# The model that answers. Chosen by this machine's memory when it was installed:
# the big one needs about 6.1 GB resident and the small one about 3.3 GB, and both
# read the same web pages and write the same cited answer. Set DECAT_MODEL here
# to switch — the model has to be pulled first: 'ollama pull qwen3-vl:8b'.
DECAT_MODEL=$MODEL
# What 'Fast' answers with, when it should differ from the above. Blank means
# Fast shares the model and is still a real choice: it reads two pages instead of
# three and skips the re-check pass, so it comes back noticeably sooner.
DECAT_FAST_MODEL=$FAST_MODEL

# How long the weights stay in memory after an answer: 10m, or 0 to give the RAM
# back at once (the next question then pays the load again).
DECAT_KEEP_ALIVE=10m

# The only third party that ever sees anything: none | ddg | brave | searxng.
DECAT_SEARCH=ddg
DECAT_SEARCH_KEY=
DECAT_SEARXNG_URL=

# Where the history of 'Public' questions is kept. 'Private' questions are never
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
# Generated by decat/mac/install.sh.
#
# Why this wrapper runs out of ~/.decat/lib instead of the repo: macOS treats
# ~/Documents as privacy-protected, and a background login item gets
# "Operation not permitted" there — no prompt, no grant, just a silent denial.
# So the package is mirrored to ~/.decat/lib (outside the protected folder)
# and PYTHONPATH points there. The mirror refreshes itself on every run from
# a terminal (which *can* read the repo); launchd runs the mirror as-is.
set -euo pipefail
REPO="$REPO"
LIB="$HOME_DIR/lib"
# Best effort, and never fatal: under launchd this read is denied by macOS
# (~/Documents is privacy-protected) and the sync silently does nothing — the
# mirror from the last terminal run is what launches. From a terminal it
# refreshes the mirror so the next launch picks up any code change.
if [ -r "\$REPO/decat/__init__.py" ]; then
  mkdir -p "\$LIB" 2>/dev/null || true
  rsync -a --delete --exclude '__pycache__' "\$REPO/decat/" "\$LIB/decat/" 2>/dev/null \\
    || cp -R "\$REPO/decat" "\$LIB/decat" 2>/dev/null \\
    || true
fi
if [ ! -f "\$LIB/decat/__init__.py" ]; then
  echo "decat: no installed copy at \$LIB — run: bash \$REPO/decat/mac/install.sh" >&2
  exit 1
fi
export PYTHONPATH="\$LIB"
if [ -f "$HOME_DIR/agent.env" ]; then
  set -a
  . "$HOME_DIR/agent.env"
  set +a
fi
cd "\$HOME" 2>/dev/null || cd /tmp
exec python3 -m decat.mac \${1+"\$@"}
EOF
chmod 755 "$BIN_DIR/decat"
say "Installed $BIN_DIR/decat (add $BIN_DIR to your PATH to type just \`decat\`)"
# Make `decat` work in new shells without a manual PATH edit.
_BIN_LINE="export PATH=\"$BIN_DIR:\$PATH\""
for _rc in "$HOME/.zprofile" "$HOME/.zshrc"; do
  if [ -f "$_rc" ]; then
    grep -qF "$BIN_DIR" "$_rc" 2>/dev/null || grep -qF '$HOME/.decat/bin' "$_rc" 2>/dev/null || printf '\n%s\n' "$_BIN_LINE" >> "$_rc"
  fi
done
if [ ! -f "$HOME/.zprofile" ] && [ ! -f "$HOME/.zshrc" ]; then
  printf '%s\n' "$_BIN_LINE" > "$HOME/.zprofile"
fi
export PATH="$BIN_DIR:$PATH"

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
  <key>WorkingDirectory</key><string>/tmp</string>
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
