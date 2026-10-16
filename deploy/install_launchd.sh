#!/usr/bin/env bash
# Daily digest, sent once per day as soon as this Mac is on (SPEC §14).
# launchd starts `radar run --scheduled` at login and every 30 minutes (also after wake);
# the program itself sends only once per day, only after NOT_BEFORE (local time), only when online.
# Usage: deploy/install_launchd.sh [NOT_BEFORE=07:00]      Remove: deploy/install_launchd.sh --uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.opportunityradar.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BOT_LABEL="com.opportunityradar.bot"
BOT_PLIST="$HOME/Library/LaunchAgents/$BOT_LABEL.plist"
RADAR="$ROOT/.venv/bin/radar"

if [[ "${1:-}" == "--uninstall" ]]; then
  for l in "$LABEL" "$BOT_LABEL"; do launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true; done
  rm -f "$PLIST" "$BOT_PLIST"
  echo "Removed the daily schedule and the bot."
  exit 0
fi

NOT_BEFORE="${1:-07:00}"

[[ "$NOT_BEFORE" =~ ^[0-2][0-9]:[0-5][0-9]$ ]] || { echo "time must be HH:MM, got '$NOT_BEFORE'" >&2; exit 1; }
[[ -x "$RADAR" ]] || { echo "missing $RADAR — run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2; exit 1; }
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RADAR</string><string>run</string><string>--scheduled</string>
    <string>--not-before</string><string>$NOT_BEFORE</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>1800</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$ROOT/data/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string></dict>
</dict>
</plist>
PLIST

# The interactive bot: always on while you're logged in, restarted if it ever stops.
cat > "$BOT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$BOT_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$RADAR</string><string>bot</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$ROOT/data/logs/bot.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/bot.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string></dict>
</dict>
</plist>
PLIST

for p in "$PLIST" "$BOT_PLIST"; do plutil -lint "$p" >/dev/null; done
for l in "$LABEL" "$BOT_LABEL"; do launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true; done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$BOT_PLIST"
echo "Installed. The digest goes out once a day, the first time this Mac is on and online after $NOT_BEFORE (local time)."
echo "Checks run at login and every 30 minutes. The Telegram bot runs whenever you're logged in."
echo "Logs: $ROOT/data/logs/"
