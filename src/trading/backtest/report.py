from __future__ import annotations


def print_report(summary: dict, *, strategy: str, start: str, end: str) -> None:
    """Print a formatted backtest performance report to stdout."""
    strat_label = strategy.upper()
    total_return = summary.get("total_return_pct", 0.0)
    ann_return = summary.get("annualized_return_pct", 0.0)
    sharpe = summary.get("sharpe_ratio", 0.0)
    max_dd = summary.get("max_drawdown_pct", 0.0)
    win_rate = summary.get("win_rate_pct", 0.0)
    total_trades = summary.get("total_trades", 0)
    wins = summary.get("wins", 0)
    avg_ret = summary.get("avg_trade_return_pct", 0.0)
    final_equity = summary.get("final_equity", 0.0)
    days = summary.get("trading_days", 0)
    run_id = summary.get("run_id")

    sign = "+" if total_return >= 0 else ""
    ann_sign = "+" if ann_return >= 0 else ""
    dd_sign = "+" if max_dd >= 0 else ""

    print()
    print(f"=== Backtest Results: {strat_label} | {start} → {end} ===")
    print(f"  Final Equity:         ${final_equity:,.2f}")
    print(f"  Total Return:         {sign}{total_return:.2f}%")
    print(f"  Annualized Return:    {ann_sign}{ann_return:.2f}%")
    print(f"  Sharpe Ratio:          {sharpe:.2f}")
    print(f"  Max Drawdown:         {dd_sign}{max_dd:.2f}%")
    print(f"  Win Rate:             {win_rate:.1f}%  ({wins}/{total_trades} closed trades)")
    print(f"  Avg Trade Return:     {('+' if avg_ret >= 0 else '')}{avg_ret:.2f}%")
    print(f"  Trading Days:         {days}")
    if run_id is not None:
        print(f"  Run ID (backtest.sqlite): {run_id}")
    print()

    # Orders by signal type
    orders_by_signal = summary.get("orders_by_signal", {})
    if orders_by_signal:
        print(f"  Orders by signal:")
        for sk, stats in sorted(orders_by_signal.items()):
            n = stats["orders"]
            cl = stats["closed"]
            w = stats["wins"]
            pnl = stats["pnl"]
            win_pct = (w / cl * 100.0) if cl else 0.0
            print(
                f"    {sk:<35s}  orders={n:3d}  closed={cl:3d}"
                f"  win={win_pct:5.1f}%  pnl={pnl:+9.2f}"
            )
        print()

    # Top 10 trades by absolute P&L
    trades = summary.get("trades_detail", [])
    closed = [t for t in trades if t.get("exit_reason") != "end_of_backtest"]
    if closed:
        closed_sorted = sorted(closed, key=lambda t: abs(t.get("realized_pnl") or 0.0), reverse=True)
        print(f"  Top trades (by |P&L|):")
        print(f"  {'Symbol':6s}  {'Entry':10s}  {'Exit':10s}  {'P&L':>9s}  {'Ret%':>7s}  {'Reason'}")
        for t in closed_sorted[:10]:
            pnl = t.get("realized_pnl") or 0.0
            ret = t.get("return_pct") or 0.0
            print(
                f"  {t['symbol']:6s}  {t['entry_date']:10s}  {t.get('exit_date','?'):10s}"
                f"  {pnl:+9.2f}  {ret:+7.2f}%  {t.get('exit_reason','')}"
            )
        print()
