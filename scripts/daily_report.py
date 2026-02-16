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
TZ = os.environ.get("TZ", "America/Denver")  # only for display; DB timestamps may be UTC

DAY = datetime.now().strftime("%Y-%m-%d")
REPORT_PATH = RPTDIR / f"{DAY}.txt"


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    cur = conn.execute(sql, params)
    return cur.fetchall()


def tail_file(path: Path, n: int = 120) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def find_today_logs() -> list[Path]:
    # matches your wrapper naming like buy_YYYY..., sell_YYYY..., daily_YYYY...
    # If you used other names, adjust this filter.
    xs = []
    if LOGDIR.exists():
        for p in sorted(LOGDIR.glob(f"*{DAY}*.log")):
            xs.append(p)
    return xs


def main() -> int:
    parts: list[str] = []
    parts.append(f"Trading Daily Report - {DAY}")
    parts.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ({TZ})")
    parts.append(f"DB: {DB}")
    parts.append("")

    conn = sqlite3.connect(DB)
    try:
        # Basic health: latest runs today (assuming runs.asof or runs.started_at exists)
        parts.append("=== Runs (today) ===")
        rows = []
        try:
            # prefer started_at if exists
            rows = q(
                conn,
                """
                SELECT id, status, asof, reason
                FROM runs
                WHERE DATE(created_at) = DATE('now')
                ORDER BY id DESC
                LIMIT 20;
                """,
            )
        except Exception:
            try:
                rows = q(
                    conn,
                    """
                    SELECT id, status, asof, reason
                    FROM runs
                    WHERE DATE(started_at) = DATE('now')
                    ORDER BY id DESC
                    LIMIT 20;
                    """,
                )
            except Exception:
                rows = []

        if rows:
            for (rid, status, asof, reason) in rows[:10]:
                parts.append(f"- run_id={rid} status={status} asof={asof} reason={reason}")
        else:
            parts.append("(runs table not readable with expected columns; skipping)")
        parts.append("")

        # Intents today (by created_at)
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
        except Exception:
            parts.append("(intents table not readable with expected created_at; skipping)")
        parts.append("")

        # Orders today
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
        except Exception:
            parts.append("(orders table not readable with expected requested_at; skipping)")
        parts.append("")

        # Snapshot (latest)
        parts.append("=== Latest Account Snapshot ===")
        try:
            r = q(
                conn,
                """
                SELECT asof, cash, equity, buying_power, long_mv
                FROM account_snapshots
                ORDER BY asof DESC
                LIMIT 1;
                """,
            )
            if r:
                asof, cash, equity, bp, long_mv = r[0]
                parts.append(f"- asof={asof} cash={cash} equity={equity} buying_power={bp} long_mv={long_mv}")
            else:
                parts.append("- none")
        except Exception:
            parts.append("(account_snapshots not available; skipping)")
        parts.append("")

        # Detect failures from logs (simple heuristic)
        parts.append("=== Errors / Warnings (from logs) ===")
        logs = find_today_logs()
        if not logs:
            parts.append("- no logs found for today in logs/")
        else:
            hit = False
            for lp in logs[-6:]:  # last few logs
                txt = tail_file(lp, n=200)
                # crude “error-ish” scan
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
