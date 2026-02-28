from __future__ import annotations

import os
import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import subprocess

from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfgen import canvas

ROOT = Path(os.environ.get("TRADING_ROOT_PATH", "C:/Users/Saphal/Desktop/Projects/Trading-system"))
LOGDIR = ROOT / "logs"
RPTDIR = ROOT / "reports"
RPTDIR.mkdir(parents=True, exist_ok=True)

DB = Path(os.environ.get("TRADING_DB_PATH", "C:/Users/Saphal/Desktop/Projects/TradingData/trading.sqlite"))
TZ = os.environ.get("TZ", "America/Denver")

DAY = datetime.now().strftime("%Y-%m-%d")
REPORT_PATH = RPTDIR / f"{DAY}.txt"
PDF_PATH = RPTDIR / f"{DAY}.pdf"

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
    if not LOGDIR.exists():
        return []
    return sorted(LOGDIR.glob(f"*{DAY}*.log"))

def summarize_jobs_from_logs(logs: list[Path]) -> list[str]:
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

def _clean_cli_text(s: str) -> str:
    """
    Clean CLI output:
    - Remove timestamp + log prefix (INFO/WARNING/ERROR)
    - Replace arrows
    - Normalize line endings
    """
    lines = []

    for raw in s.splitlines():
        line = raw.strip()

        for marker in (" | INFO | trading | ", " | WARNING | trading | ", " | ERROR | trading | "):
            if marker in line:
                line = line.split(marker, 1)[1]
                break

        line = line.replace("\\u2192", "->").replace("→", "->")
        lines.append(line)

    return "\n".join(lines).strip()

def run_trading_cli(args: list[str], *, timeout_s: int = 45) -> str:
    """
    Run: python -m trading <args...>
    Returns stdout+stderr (cleaned). Never raises.
    """
    cmd = [sys.executable, "-m", "trading"] + args
    try:
        p = subprocess.run(
            cmd,
            cwd=str(ROOT),                
            env=os.environ.copy(),   
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        out = _clean_cli_text(p.stdout or "")
        if p.returncode != 0:
            return f"(command failed rc={p.returncode})\n$ {' '.join(cmd)}\n{out}"
        return out
    except subprocess.TimeoutExpired:
        return f"(command timed out after {timeout_s}s)\n$ {' '.join(cmd)}"
    except Exception as e:
        return f"(command error: {e})\n$ {' '.join(cmd)}"


def get_performance_output() -> str:
    return run_trading_cli(["performance"], timeout_s=45)

def analytics_blocks() -> list[tuple[str, list[str], int]]:
    """
    (title, cli_args, timeout_s)

    Tune these without touching the report formatting.
    """
    # “Since” for realized attribution windows (default 90d in your CLI, but we can be explicit)
    since_180 = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")

    return [
        ("Signal Performance (total: realized + open)", ["perf-by-signal-total", "--since", since_180], 45),
        ("Signal Performance (open/unrealized)", ["perf-by-signal-open"], 45),

        # Attribution (these can be heavier with --mfe; give them more time)
        ("Attribution: Entry+Exit Combos (MFE/MAE)", ["attrib-combo", "--mfe", "--top", "15"], 90),
        ("Attribution: Entry Signals (MFE/MAE)", ["attrib-entry", "--top", "20", "--show", "0"], 45),
        ("Attribution: Exit Rules (MFE/MAE)", ["attrib-exit", "--mfe", "--top", "20", "--show", "0"], 90),

        # Portfolio state
        ("Exposure (top holdings by MV)", ["exposure", "--top", "20"], 45),

        # Optional: if you like this, keep it. If it’s noisy, remove it.
        ("PnL Snapshot (realized/unrealized)", ["pnl"], 45),
    ]

def _parse_top_signal_from_perf_by_signal_total(txt: str) -> tuple[str, str] | None:
    """
    Returns (signal_key, total_pnl) from perf-by-signal-total output.
    We look for the first line that contains 'trades=' and 'rpnl=' or 'pnl='.
    """
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            continue

        if s.startswith("PERF-BY-SIGNAL-TOTAL"):
            continue
        if "trades=" in s and ("rpnl=" in s or "pnl=" in s):
            
            key = s.split()[0]
            total = None
            if "total=" in s:
                total = s.split("total=", 1)[1].split()[0]
            elif "rpnl=" in s:
                total = s.split("rpnl=", 1)[1].split()[0]
            return (key, total or "?")
    return None

def _parse_worst_exit_from_attrib_exit(txt: str) -> tuple[str, str] | None:
    """
    Returns (exit_key, total_pnl) for the worst (most negative total_pnl) exit rule.
    We'll parse lines that look like exit rule rows and pick min total_pnl.
    """
    worst: tuple[str, float] | None = None

    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            continue
        # likely rows start with exit_
        if not s.startswith("exit_"):
            continue

        parts = s.split()
        exit_key = parts[0]

        # total_pnl appears later in the row; we find the first token that parses as float with sign
        pnl_val = None
        for tok in parts[1:]:
            t = tok.replace("+", "")
            try:
                pnl_val = float(t)
                break
            except Exception:
                continue

        if pnl_val is None:
            continue

        if (worst is None) or (pnl_val < worst[1]):
            worst = (exit_key, pnl_val)

    if worst is None:
        return None

    exit_key, pnl = worst
    pnl_str = f"{pnl:+.2f}"
    return (exit_key, pnl_str)

def _parse_top_weight_from_exposure(txt: str) -> tuple[str, str] | None:
    """
    Returns (symbol, weight_pct_str) from exposure output.
    Looks for the first position line with '(xx.xx%)'.
    """
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            continue

        if s.startswith("EXPOSURE"):
            continue

        if "(" in s and "%)" in s:
            sym = s.split()[0]
            inside = s.split("(", 1)[1].split(")", 1)[0]  # "18.99%"
            if "%" in inside:
                return (sym, inside)
    return None

def build_executive_summary(conn: sqlite3.Connection) -> list[str]:
    lines: list[str] = []
    lines.append("=" * 90)
    lines.append("EXECUTIVE SUMMARY")
    lines.append("=" * 90)

    # Latest account snapshot
    row = conn.execute("""
        SELECT asof_date, cash, equity, buying_power, long_market_value
        FROM account_snapshots_daily
        ORDER BY asof_date DESC
        LIMIT 1
    """).fetchone()

    if row:
        asof_date, cash, equity, bp, long_mv = row
        exposure = (long_mv / equity * 100) if equity else 0.0

        lines.append(f"As Of:                 {asof_date}")
        lines.append(f"Equity:                ${equity:,.2f}")
        lines.append(f"Cash:                  ${cash:,.2f}")
        lines.append(f"Buying Power:          ${bp:,.2f}")
        lines.append(f"Exposure (LMV/Equity):  {exposure:.1f}%")
        lines.append("")

    # Performance headline (uses your 'performance' command)
    perf = run_trading_cli(["performance"], timeout_s=45)
    cum = None
    dd = None
    for l in perf.splitlines():
        if "PERFORMANCE" in l:
            if "cum=" in l:
                try:
                    cum = l.split("cum=", 1)[1].split("%", 1)[0] + "%"
                except Exception:
                    pass
            if "maxDD=" in l:
                try:
                    dd = l.split("maxDD=", 1)[1].split("%", 1)[0] + "%"
                except Exception:
                    pass

    if cum or dd:
        lines.append("Performance:")
        if cum:
            lines.append(f"180d Return:           {cum}")
        if dd:
            lines.append(f"Max Drawdown:          {dd}")
        lines.append("")

    # Enhanced insights (180d)
    since_180 = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")

    # Top Signal (total realized + open)
    sig_txt = run_trading_cli(["perf-by-signal-total", "--since", since_180], timeout_s=45)
    top_sig = _parse_top_signal_from_perf_by_signal_total(sig_txt)

    # Worst Exit Rule (realized attribution)
    exit_txt = run_trading_cli(["attrib-exit", "--mfe", "--top", "50", "--show", "0"], timeout_s=90)
    worst_exit = _parse_worst_exit_from_attrib_exit(exit_txt)

    # Top position concentration
    exp_txt = run_trading_cli(["exposure", "--top", "10"], timeout_s=45)
    top_weight = _parse_top_weight_from_exposure(exp_txt)

    lines.append("Key Insights (180d):")
    if top_sig:
        lines.append(f"Top Signal:            {top_sig[0]}   (total_pnl={top_sig[1]})")
    else:
        lines.append("Top Signal:            (n/a)")

    if worst_exit:
        lines.append(f"Worst Exit Rule:       {worst_exit[0]}   (total_pnl={worst_exit[1]})")
    else:
        lines.append("Worst Exit Rule:       (n/a)")

    if top_weight:
        lines.append(f"Top Position Weight:   {top_weight[0]}   ({top_weight[1]})")
    else:
        lines.append("Top Position Weight:   (n/a)")

    lines.append("=" * 90)
    lines.append("")

    return lines

def write_pdf(report_text: str, pdf_path: Path) -> None:
    """
    Render full report in landscape orientation for wide analytics tables.
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(pdf_path), pagesize=landscape(letter))
    width, height = landscape(letter)

    left = 20
    top = height - 36
    bottom = 36
    line_h = 10

    c.setTitle(pdf_path.name)
    c.setFont("Courier", 8) 

    y = top

    for raw in report_text.splitlines():
        line = raw.rstrip("\n")

        max_chars = 180
        chunks = [""] if not line else [
            line[i:i + max_chars] for i in range(0, len(line), max_chars)
        ]

        for ch in chunks:
            if y <= bottom:
                c.showPage()
                c.setFont("Courier", 8)
                y = top

            c.drawString(left, y, ch)
            y -= line_h

    c.save()

def main() -> int:

    conn = sqlite3.connect(DB)

    parts: list[str] = []
    parts.append(f"Trading Daily Report - {DAY}")
    parts.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ({TZ})")
    parts.append(f"DB: {DB}")
    parts.append("")

    parts.extend(build_executive_summary(conn))

    try:
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

        parts.append("=== Performance ===")
        parts.append(get_performance_output())
        parts.append("")

        parts.append("=== Analytics ===")
        for title, cli_args, t in analytics_blocks():
            parts.append(f"\n--- {title} ---")
            parts.append(run_trading_cli(cli_args, timeout_s=t))
        parts.append("")

    finally:
        conn.close()

    report_text = "\n".join(parts) + "\n"
    REPORT_PATH.write_text(report_text, encoding="utf-8")
    write_pdf(report_text, PDF_PATH)
    print(str(REPORT_PATH))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())