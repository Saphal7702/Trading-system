import os
from pathlib import Path
import requests

def send_telegram_message(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)

    if not resp.ok:
    	raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")

    resp.raise_for_status()

def main() -> int:
    report_path = os.getenv("TRADING_REPORT_PATH")
    if not report_path:
        raise RuntimeError("Missing TRADING_REPORT_PATH")

    body = Path(report_path).read_text(encoding="utf-8", errors="replace")

    # Telegram limit ~4096 chars; keep safe
    if len(body) > 3800:
        body = body[:3800] + "\n\n(Report truncated. Full report is on the server.)"

    send_telegram_message(body)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
