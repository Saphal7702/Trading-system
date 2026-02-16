#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/saphal7702/Trading/Trading-system"
VENV="$ROOT/.venv"
ENVFILE="/home/saphal7702/Trading/Trading-system/.env"
export TZ="America/Denver"

if [ -f "$ENVFILE" ]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENVFILE"
  set +a
fi

cd "$ROOT"
source "$VENV/bin/activate"

# Generate report
REPORT_PATH="$(python "$ROOT/scripts/daily_report.py")"
export TRADING_REPORT_PATH="$REPORT_PATH"

# Send to Telegram (optional)
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  python "$ROOT/scripts/telegram.py"
else
  echo "Telegram env not set; report generated at: $REPORT_PATH"
fi
