#!/usr/bin/env bash
set -euo pipefail

# Usage: ./run_job.sh [data|buy|sell]
JOB="${1:-buy}"

# ROOT = repo root (two levels up if script is in scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENVFILE="$ROOT/.env"
if [ -f "$ENVFILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENVFILE"
  set +a
fi

ENV_NAME="${TRADING_ENV:-paper}"
BASE="/home/saphal7702/Trading"

case "$ENV_NAME" in
  paper)
    DEFAULT_DATA="$BASE/Trading-paper/TradingData"
    ;;
  live)
    DEFAULT_DATA="$BASE/Trading-live/TradingData"
    ;;
  *)
    echo "Unknown TRADING_ENV: $ENV_NAME (use: paper|live)"
    exit 2
    ;;
esac

DATA_DIR="${TRADING_DATA_PATH:-$DEFAULT_DATA}"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"

export TZ="${TZ:-America/Denver}"
export TRADING_ROOT_PATH="$ROOT"
export TRADING_DATA_PATH="$DATA_DIR"
export TRADING_ENV="$ENV_NAME"

export TRADING_DB_PATH="${TRADING_DB_PATH:-$DATA_DIR/trading.sqlite}"

cd "$ROOT"

if [ ! -x "$PY" ]; then
  echo "ERROR: venv python not found: $PY"
  echo "Fix:"
  echo "  cd '$ROOT'"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/python -m pip install -U pip setuptools wheel"
  echo "  .venv/bin/python -m pip install -e ."
  exit 1
fi

if ! "$PY" -c "import trading" >/dev/null 2>&1; then
  echo "ERROR: 'trading' not installed in venv: $VENV"
  echo "Fix: cd '$ROOT' && '$PY' -m pip install -e ."
  exit 1
fi

ts="$(date '+%Y-%m-%d_%H%M%S')"
log="$LOGDIR/${ENV_NAME}_${JOB}_${ts}.log"

run() {
  echo "[$(date '+%F %T')] CMD: $*" | tee -a "$log"
  "$@" 2>&1 | tee -a "$log"
}

case "$JOB" in
  data)
    run "$PY" -m trading fetch-bars --days 400
    ;;
  buy)
    run "$PY" -m trading preflight || {
      echo "[$(date '+%F %T')] Preflight blocked; skipping run-once." | tee -a "$log"
      exit 0
    }
    run "$PY" -m trading run-once --execute
    ;;
  sell)
    run "$PY" -m trading preflight || {
      echo "[$(date '+%F %T')] Preflight blocked; skipping exits/run-once." | tee -a "$log"
      exit 0
    }
    run "$PY" -m trading exits --emit-intents
    run "$PY" -m trading run-once --execute
    ;;
  *)
    echo "Unknown job: $JOB (use: data|buy|sell)" | tee -a "$log"
    exit 2
    ;;
esac