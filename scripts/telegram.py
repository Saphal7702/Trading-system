import os
from pathlib import Path
import requests


def _safe_int(x: str) -> int | None:
    try:
        return int(x.strip())
    except Exception:
        return None


def _build_brief_summary(report_text: str) -> str:
    """Create a short, Telegram-friendly daily summary."""

    day = ""
    lines = report_text.splitlines()
    if lines and "Trading Daily Report" in lines[0]:
        # "Trading Daily Report - YYYY-MM-DD"
        day = lines[0].split("-", 1)[-1].strip()

    # Runs
    runs_total = 0
    runs_success = 0

    # Intents
    buy_cnt = None
    sell_cnt = None
    hold_cnt = None

    # Account snapshot
    equity = None
    cash = None
    bp = None

    # Performance headline
    cum_ret = None
    max_dd = None

    # Warnings / errors
    warnings: list[str] = []
    has_errors = False

    section = None
    for ln in lines:
        if ln.startswith("=== ") and ln.endswith(" ==="):
            section = ln
            continue

        if section == "=== Runs (today) ===":
            if ln.startswith("- run_id="):
                runs_total += 1
                if "status=success" in ln:
                    runs_success += 1

        elif section == "=== Intents (today) ===":
            # lines like: "- buy: 2"
            if ln.startswith("- ") and ":" in ln:
                k, v = ln[2:].split(":", 1)
                n = _safe_int(v)
                if n is None:
                    continue
                k = k.strip().lower()
                if k == "buy":
                    buy_cnt = n
                elif k == "sell":
                    sell_cnt = n
                elif k == "hold":
                    hold_cnt = n

        elif section == "=== Latest Account Snapshot ===":
            # line like: "- asof_date=... cash=35.26 equity=... buying_power=..."
            if ln.startswith("- "):
                for part in ln.split():
                    if part.startswith("cash="):
                        cash = part.split("=", 1)[1]
                    elif part.startswith("equity="):
                        equity = part.split("=", 1)[1]
                    elif part.startswith("buying_power="):
                        bp = part.split("=", 1)[1]

        elif section == "=== Jobs (today, from logs) ===":
            if "PREFLIGHT" in ln and "blockers=[" in ln and "blockers=[]" not in ln:
                job = None
                if ln.lstrip().startswith("- buy:"):
                    job = "Buy"
                elif ln.lstrip().startswith("- sell:"):
                    job = "Sell"

                if "insufficient_buying_power" in ln:
                    if job == "Sell":
                        msg = "⚠ Sell step skipped (min buying power gate)"
                    else:
                        msg = "⚠ Buy blocked (insufficient buying power)"
                    if msg not in warnings:
                        warnings.append(msg)

                elif "market_open=False" in ln:
                    msg = "⚠ Market closed"
                    if msg not in warnings:
                        warnings.append(msg)

                else:
                    msg = f"⚠ {job + ' ' if job else ''}preflight blockers present"
                    if msg not in warnings:
                        warnings.append(msg)

        elif section == "=== Performance ===":
            if "| INFO | trading | PERFORMANCE" in ln:
                if "cum=" in ln:
                    try:
                        cum_ret = ln.split("cum=", 1)[1].split("%", 1)[0] + "%"
                    except Exception:
                        pass
                if "maxDD=" in ln:
                    try:
                        max_dd = ln.split("maxDD=", 1)[1].split("%", 1)[0] + "%"
                    except Exception:
                        pass

        elif section == "=== Errors / Warnings (from logs) ===":
            if ln.startswith("- no obvious errors"):
                continue
            if "Traceback (most recent call last)" in ln or " ERROR " in ln or "Exception:" in ln:
                has_errors = True

    # Defaults if missing
    env = os.getenv("TRADING_ENV")
    buy_cnt = buy_cnt if buy_cnt is not None else 0
    sell_cnt = sell_cnt if sell_cnt is not None else 0
    hold_cnt = hold_cnt if hold_cnt is not None else 0

    title_day = day or "(unknown date)"
    out: list[str] = []
    out.append(f"📊 Trading Daily Report — {title_day}")
    out.append(f"System — {env}")
    out.append("")
    out.append(f"Runs: {runs_success}/{runs_total} success")
    out.append(f"Buys: {buy_cnt}")
    out.append(f"Sells: {sell_cnt}")
    out.append(f"Holds: {hold_cnt}")
    out.append("")

    if equity is not None or cash is not None or bp is not None:
        out.append("Account:")
        if equity is not None:
            out.append(f"Equity: ${equity}")
        if cash is not None:
            out.append(f"Cash: ${cash}")
        if bp is not None:
            out.append(f"Buying Power: ${bp}")
        out.append("")

    if cum_ret is not None or max_dd is not None:
        out.append("Performance:")
        if cum_ret is not None:
            out.append(f"Cumulative: {cum_ret}")
        if max_dd is not None:
            out.append(f"Max DD: {max_dd}")
        out.append("")

    out.append("System Status:")
    if has_errors:
        out.append("❌ Errors detected (see attached PDF)")
    else:
        if warnings:
            out.extend(warnings[:3])
        out.append("✅ No system errors")

    return "\n".join(out).strip() + "\n"


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


def send_telegram_document(*, file_path: str, caption: str | None = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    p = Path(file_path)
    if not p.exists():
        raise RuntimeError(f"PDF not found: {file_path}")

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with p.open("rb") as f:
        files = {"document": (p.name, f, "application/pdf")}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        resp = requests.post(url, data=data, files=files, timeout=30)

    if not resp.ok:
        raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")

    resp.raise_for_status()


def main() -> int:
    report_path = os.getenv("TRADING_REPORT_PATH")
    if not report_path:
        raise RuntimeError("Missing TRADING_REPORT_PATH")

    pdf_path = os.getenv("TRADING_PDF_PATH")

    body = Path(report_path).read_text(encoding="utf-8", errors="replace")

    # 1) brief summary message
    summary = _build_brief_summary(body)
    if len(summary) > 3800:
        summary = summary[:3800] + "\n"
    send_telegram_message(summary)

    # 2) attach PDF (preferred)
    if pdf_path:
        send_telegram_document(file_path=pdf_path, caption="Detailed PDF report")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())