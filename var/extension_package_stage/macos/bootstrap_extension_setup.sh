#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_ID="com.printingbusiness.flask"
PLIST_PATH="$LAUNCH_AGENTS_DIR/${LAUNCH_AGENT_ID}.plist"
LOG_DIR="$PROJECT_ROOT/var"
STDOUT_LOG="$LOG_DIR/flask_mac_stdout.log"
STDERR_LOG="$LOG_DIR/flask_mac_stderr.log"
START_SCRIPT="$PROJECT_ROOT/start_flask_background.sh"

mkdir -p "$LOG_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"

echo "=========================================="
echo "MakerWorld Extension Bootstrap (macOS)"
echo "=========================================="
echo "Project root: $PROJECT_ROOT"

echo ""
echo "Enter required values. They will be stored in your local .env file."

read -r -p "DATABASE_URL: " DATABASE_URL
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL is required." >&2
  exit 1
fi

read -r -p "SECRET_KEY (press Enter to auto-generate): " SECRET_KEY
if [[ -z "${SECRET_KEY:-}" ]]; then
  SECRET_KEY="$(LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 96 || python3 -c 'import secrets; print(secrets.token_hex(48))')"
  echo "SECRET_KEY auto-generated."
fi

# Keep admin credential internal for extension-user installs.
ADMIN_PASSWORD="2011admin"

read -r -p "EXTENSION_API_KEY (optional): " EXTENSION_API_KEY

ENV_FILE="$PROJECT_ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  touch "$ENV_FILE"
fi

upsert_env() {
  local key="$1"
  local value="$2"
  local escaped
  escaped="$(printf '%s' "$value" | sed -e 's/[\\&]/\\\\&/g')"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i '' -E "s|^${key}=.*|${key}=${escaped}|" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

upsert_env "DATABASE_URL" "$DATABASE_URL"
upsert_env "SECRET_KEY" "$SECRET_KEY"
upsert_env "ADMIN_PASSWORD" "$ADMIN_PASSWORD"
if [[ -n "${EXTENSION_API_KEY:-}" ]]; then
  upsert_env "EXTENSION_API_KEY" "$EXTENSION_API_KEY"
fi

if [[ ! -x "$START_SCRIPT" ]]; then
  chmod +x "$START_SCRIPT"
fi

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LAUNCH_AGENT_ID}</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${START_SCRIPT}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${STDOUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${STDERR_LOG}</string>
  </dict>
</plist>
PLIST

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

/bin/bash "$START_SCRIPT" || true

echo ""
echo "Setup complete. LaunchAgent registered: $PLIST_PATH"
echo "To disable auto-start later, run: ./disable_flask_autostart.sh"
echo "Hosted portal fallback: https://print-portal-qm9p.onrender.com/"
