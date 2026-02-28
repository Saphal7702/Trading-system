#!/usr/bin/env bash
set -euo pipefail

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    DEFAULT_ROOT="/c/Users/Saphal/Desktop/Projects/Trading-system"
    VENV_ACTIVATE="Scripts/activate"
    VENV_PY_REL="Scripts/python"
else
    DEFAULT_ROOT="/home/saphal7702/Trading/Trading-paper/Trading-system"
    VENV_ACTIVATE="bin/activate"
    VENV_PY_REL="bin/python"
fi

export ROOT="${TRADING_ROOT_PATH:-$DEFAULT_ROOT}"
VENV="$ROOT/.venv"
ENVFILE="$ROOT/.env"
export TZ="America/Denver"

if [ -f "$ENVFILE" ]; then
  set -a
  source "$ENVFILE"
  set +a
fi

cd "$ROOT"

if [ -f "$VENV/$VENV_ACTIVATE" ]; then
    source "$VENV/$VENV_ACTIVATE"
else
    echo "Error: Virtual environment not found at $VENV/$VENV_ACTIVATE"
    exit 1
fi

# Always use venv python explicitly (no PATH ambiguity)
PYTHON_EXE="$VENV/$VENV_PY_REL"
if [ ! -x "$PYTHON_EXE" ]; then
  echo "Error: Python executable not found at $PYTHON_EXE"
  exit 1
fi

# Generate report
REPORT_PATH="$("$PYTHON_EXE" "$ROOT/scripts/daily_report.py")"
export TRADING_REPORT_PATH="$REPORT_PATH"

PDF_PATH="${REPORT_PATH%.txt}.pdf"
export TRADING_PDF_PATH="$PDF_PATH"

# Send to Telegram
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  "$PYTHON_EXE" "$ROOT/scripts/telegram.py"
else
  echo "Telegram env not set; report generated at: $REPORT_PATH"
fi