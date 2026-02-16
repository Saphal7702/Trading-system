from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path("/home/saphal7702/Trading/Trading-system")
LOGDIR = ROOT / "logs"
RPTDIR = ROOT / "reports"
RPTDIR.mkdir(parents=True, exist_ok=True)

DB = os.environ.get("TRADING_DB_PATH", "/home/saphal7702/Trading/TradingData/trading.sqlite")
TZ = os.environ.get("TZ", "America/Denver")

DAY = datetime.now().strftime("%Y-%m-%d")
REPORT_PATH = RPTDIR / f"{DAY}.txt"


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    cur = conn.execute(sql, params)
    return cur.fetchall()


def tail_file(path: Path, n: int = 160) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def find_today_logs() -> list[Path]:
    # matches buy_YYYY-MM-DD_*.log, sell_YYYY-MM-DD_*.log, data_YYYY-MM-DD_*.log
    if not LOGDIR.exists():
        return []
    return sorted(LOGDIR.glob(f"*{DAY}*.log"))

def summarize_jobs_from_logs(logs: list[Path]) -> list[str]:
    # Keep only the latest line per job (data/buy/sell)
    latest: dict[str, str] = {}

    for lp in logs:
        name = lp.name
        txt = tail_file(lp, n=220)

        job = None
        if name.startswith("data_"):
            job = "data"
        elif name.startswith("buy_"):
            job = "buy"
        elif name.startswith("sell_"):
            job = "sell"
        else:
            continue

        if job == "data":
            fetched = [ln for ln in txt.splitlines() if "Fetched bars:" in ln]
            if fetched:
                latest[job] = f"- data: OK ({fetched[-1].strip()})"
            else:
                latest[job] = "- data: ran (no fetched summary found)"
        else:
            pre = [ln for ln in txt.splitlines() if "PREFLIGHT" in ln]
            if pre:
                latest[job] = f"- {job}: {pre[-1].strip()}"
            else:
                latest[job] = f"- {job}: ran (no PREFLIGHT line found)"

    out: list[str] = []
    for job in ("data", "buy", "sell"):
        if job in latest:
            out.append(latest[job])
    return out

def main() -> int:
    parts: list[str] = []
    parts.append(f"Trading Daily Report - {DAY}")
    parts.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ({TZ})")
    parts.append(f"DB: {DB}")
    parts.append("")

    conn = sqlite3.connect(DB)
    try:
        # Runs today (your schema: started_at, asof_date, notes)
        parts.append("=== Runs (today) ===")
        try:
            rows = q(
                conn,
                """
                SELECT id, status, asof_date, reason, started_at, finished_at
                FROM runs
                WHERE DATE(started_at) = DATE('now')
                ORDER BY id DESC
                LIMIT 20;
                """,
            )
            if rows:
                for rid, status, asof_date, reason, started_at, finished_at in rows[:10]:
                    parts.append(
                        f"- run_id={rid} status={status} asof_date={asof_date} "
                        f"started_at={started_at} finished_at={finished_at} reason={reason}"
                    )
            else:
                parts.append("- none")
        except Exception as e:
            parts.append(f"(runs query failed: {e})")
        parts.append("")

        # Intents today (your schema: created_at exists)
        parts.append("=== Intents (today) ===")
        try:
            rows = q(
                conn,
                """
                SELECT action, COUNT(*)
                FROM intents
                WHERE DATE(created_at) = DATE('now')
                GROUP BY action
                ORDER BY action;
                """,
            )
            if rows:
                for action, n in rows:
                    parts.append(f"- {action}: {n}")
            else:
                parts.append("- none")
        except Exception as e:
            parts.append(f"(intents query failed: {e})")
        parts.append("")

        # Orders today (your schema: requested_at exists)
        parts.append("=== Orders (today) ===")
        try:
            rows = q(
                conn,
                """
                SELECT side, status, COUNT(*)
                FROM orders
                WHERE DATE(requested_at) = DATE('now')
                GROUP BY side, status
                ORDER BY side, status;
                """,
            )
            if rows:
                for side, status, n in rows:
                    parts.append(f"- {side} {status}: {n}")
            else:
                parts.append("- none")
        except Exception as e:
            parts.append(f"(orders query failed: {e})")
        parts.append("")

        # Snapshot (your schema: account_snapshots_daily)
        parts.append("=== Latest Account Snapshot ===")
        try:
            r = q(
                conn,
                """
                SELECT asof_date, cash, equity, buying_power, long_market_value, run_id
                FROM account_snapshots_daily
                ORDER BY asof_date DESC
                LIMIT 1;
                """,
            )
            if r:
                asof_date, cash, equity, bp, long_mv, run_id = r[0]
                parts.append(
                    f"- asof_date={asof_date} cash={cash} equity={equity} "
                    f"buying_power={bp} long_mv={long_mv} run_id={run_id}"
                )
            else:
                parts.append("- none")
        except Exception as e:
            parts.append(f"(account_snapshots_daily not available: {e})")
        parts.append("")

        parts.append("=== Jobs (today, from logs) ===")
        logs = find_today_logs()
        if not logs:
            parts.append("- none")
        else:
            parts.extend(summarize_jobs_from_logs(logs))
        parts.append("")

        # Errors / warnings from logs
        parts.append("=== Errors / Warnings (from logs) ===")
        logs = find_today_logs()
        if not logs:
            parts.append("- no logs found for today in logs/")
        else:
            hit = False
            for lp in logs[-8:]:
                txt = tail_file(lp, n=220)
                if ("Traceback (most recent call last)" in txt) or ("ERROR" in txt) or ("Exception:" in txt):
                    hit = True
                    parts.append(f"\n--- {lp.name} (tail) ---\n{txt}\n")
            if not hit:
                parts.append("- no obvious errors found in today's log tails")
        parts.append("")

    finally:
        conn.close()

    REPORT_PATH.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(str(REPORT_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
