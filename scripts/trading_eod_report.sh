#!/usr/bin/env bash
set -euo pipefail

# 1. Detect OS and set appropriate default Root Path
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    # Windows (Git Bash / MSYS)
    DEFAULT_ROOT="/c/Users/Saphal/Desktop/Projects/Trading-system"
    VENV_ACTIVATE="Scripts/activate"
else
    # Linux / macOS
    DEFAULT_ROOT="/home/saphal7702/Trading/Trading-system" 
    VENV_ACTIVATE="bin/activate"
fi

export ROOT="${TRADING_ROOT_PATH:-$DEFAULT_ROOT}"
VENV="$ROOT/.venv"
ENVFILE="$ROOT/.env"
export TZ="America/Denver"

# 2. Load Environment Variables
if [ -f "$ENVFILE" ]; then
  set -a
  source "$ENVFILE"
  set +a
fi

cd "$ROOT"

# 3. Activate Virtual Environment based on OS structure
if [ -f "$VENV/$VENV_ACTIVATE" ]; then
    source "$VENV/$VENV_ACTIVATE"
else
    echo "Error: Virtual environment not found at $VENV/$VENV_ACTIVATE"
    exit 1
fi

# 4. Generate report
PYTHON_EXE="python"
if [[ "$OSTYPE" != "msys" && "$OSTYPE" != "cygwin" ]]; then
    PYTHON_EXE="python3"
fi

# Generate report
REPORT_PATH="$(python "$ROOT/scripts/daily_report.py")"
export TRADING_REPORT_PATH="$REPORT_PATH"

# PDF path is generated alongside the txt report
PDF_PATH="${REPORT_PATH%.txt}.pdf"
export TRADING_PDF_PATH="$PDF_PATH"

# 5. Send to Telegram
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  $PYTHON_EXE "$ROOT/scripts/telegram.py" # <--- Fixed to match PYTHON_EXE
else
  echo "Telegram env not set; report generated at: $REPORT_PATH"
fi