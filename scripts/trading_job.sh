#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/saphal7702/Trading/Trading-system"
VENV="$ROOT/.venv"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"

# Make logs predictable in Denver time
export TZ="America/Denver"

# Load secrets/env (recommended) if present
# Put Alpaca keys etc. in this file (chmod 600).
ENVFILE="/home/saphal7702/.trading_env"
if [ -f "$ENVFILE" ]; then
  # shellcheck disable=SC1090
  source "$ENVFILE"
fi

# DB path (EDIT THIS to your actual sqlite location)
export TRADING_DB_PATH="${TRADING_DB_PATH:-/home/saphal7702/Trading/TradingData/trading.sqlite}"

cd "$ROOT"
source "$VENV/bin/activate"

job="${1:-buy}"
ts="$(date '+%Y-%m-%d_%H%M%S')"
log="$LOGDIR/${job}_${ts}.log"

run() {
  echo "[$(date '+%F %T')] CMD: $*" | tee -a "$log"
  "$@" 2>&1 | tee -a "$log"
}

# Use python -m trading to avoid PATH issues under cron
case "$job" in
  data)
    run python -m trading fetch-bars --days 10
    ;;
  buy)
    run python -m trading preflight
    run python -m trading run-once --execute
    ;;
  sell)
    run python -m trading preflight
    run python -m trading exits --emit-intents
    run python -m trading run-once --execute
    ;;
  *)
    echo "Unknown job: $job (use: data|buy|sell)" | tee -a "$log"
    exit 2
    ;;
esac
