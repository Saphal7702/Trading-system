"""
Tests for src/trading/analysis/slippage.py

Each test builds a minimal in-memory SQLite via the db fixture (from conftest.py),
inserts synthetic fixtures, then calls analyze_slippage() and asserts on the
returned dict.
"""
from __future__ import annotations

import pytest
from trading.analysis.slippage import SlippageConfig, analyze_slippage


# ── helpers ───────────────────────────────────────────────────────────────────

def _setup_chain(conn, *, symbol="AAPL", signal_date="2026-01-02",
                 fill_date="2026-01-03", side="buy",
                 signal_close=100.0, fill_price=101.0, qty=10.0,
                 fill_bar_lo=100.5, fill_bar_hi=102.0, fill_bar_o=100.8):
    """
    Insert the minimum rows needed for one clean analyzed execution.
    Returns (run_id, intent_id, order_id, exec_id).
    """
    # bars: signal date bar
    conn.execute(
        "INSERT OR IGNORE INTO bars_daily(symbol,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
        (symbol, signal_date, signal_close, signal_close, signal_close, signal_close, 1_000_000),
    )
    # bars: fill date bar
    conn.execute(
        "INSERT OR IGNORE INTO bars_daily(symbol,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
        (symbol, fill_date, fill_bar_o, fill_bar_hi, fill_bar_lo, fill_price, 1_000_000),
    )

    conn.execute(
        "INSERT INTO runs(asof_date, started_at, status) VALUES(?,datetime('now'),'success')",
        (signal_date,),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO intents(run_id, symbol, action, strength, reason) VALUES(?,?,?,1.0,'test')",
        (run_id, symbol, side),
    )
    intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO orders(symbol, side, qty, status, requested_at, intent_id) "
        "VALUES(?,?,?,?,datetime('now'),?)",
        (symbol, side, qty, "filled", intent_id),
    )
    order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO executions(broker,broker_order_id,symbol,side,qty,filled_avg_price,filled_at,order_id) "
        "VALUES('alpaca-paper','BRK001',?,?,?,?,?,?)",
        (symbol, side, qty, fill_price, f"{fill_date}T09:30:00Z", order_id),
    )
    exec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    return run_id, intent_id, order_id, exec_id


# ── test 1: standard case ─────────────────────────────────────────────────────

def test_standard_case(db, monkeypatch):
    from trading.db import connect
    with connect() as conn:
        _setup_chain(
            conn,
            symbol="AAPL",
            signal_date="2026-01-02",
            fill_date="2026-01-03",
            side="buy",
            signal_close=100.0,
            fill_price=101.0,
            qty=10.0,
        )

    result = analyze_slippage(SlippageConfig())
    assert result["analyzed"] == 1
    row = result["rows"][0]
    assert row["symbol"] == "AAPL"
    assert row["signal_close"] == pytest.approx(100.0)
    assert row["fill_price"] == pytest.approx(101.0)
    assert row["slippage_abs"] == pytest.approx(1.0)
    assert row["slippage_pct"] == pytest.approx(1.0)


# ── test 2: buy slippage is signed positive when fill > signal close ──────────

def test_buy_slippage_signed_positive(db):
    from trading.db import connect
    with connect() as conn:
        _setup_chain(conn, side="buy", signal_close=100.0, fill_price=102.0)

    result = analyze_slippage(SlippageConfig())
    row = result["rows"][0]
    assert row["signed_slippage_pct"] > 0
    assert row["signed_slippage_pct"] == pytest.approx(2.0)


# ── test 3: sell slippage is signed positive when fill < signal close ─────────

def test_sell_slippage_signed_positive(db):
    from trading.db import connect
    with connect() as conn:
        _setup_chain(
            conn,
            symbol="MSFT",
            side="sell",
            signal_close=200.0,
            fill_price=198.0,   # sold below close → unfavorable
            fill_bar_lo=197.0,
            fill_bar_hi=201.0,
            fill_bar_o=199.0,
        )

    result = analyze_slippage(SlippageConfig())
    row = result["rows"][0]
    # slippage_pct = (198-200)/200 * 100 = -1.0 ; signed = -(-1.0) = +1.0
    assert row["signed_slippage_pct"] == pytest.approx(1.0)


# ── test 4: multi-day timing gap ──────────────────────────────────────────────

def test_timing_gap_multi_day(db):
    from trading.db import connect
    with connect() as conn:
        _setup_chain(
            conn,
            signal_date="2026-01-02",
            fill_date="2026-01-06",   # 4 calendar days later
            fill_bar_lo=98.0,
            fill_bar_hi=103.0,
            fill_bar_o=100.5,
        )

    result = analyze_slippage(SlippageConfig())
    row = result["rows"][0]
    assert row["timing_gap_days"] == 4


# ── test 5: fill_inside_bar False when fill > fill_high ───────────────────────

def test_fill_outside_bar_flagged(db):
    from trading.db import connect
    with connect() as conn:
        _setup_chain(
            conn,
            signal_close=100.0,
            fill_price=105.0,
            fill_bar_lo=100.0,
            fill_bar_hi=104.0,   # fill > bar high → flagged
            fill_bar_o=100.5,
        )

    result = analyze_slippage(SlippageConfig())
    row = result["rows"][0]
    assert row["fill_inside_bar"] is False


# ── test 6: missing signal bar → skipped ─────────────────────────────────────

def test_missing_signal_bar_skipped(db):
    from trading.db import connect
    with connect() as conn:
        # Insert fill-date bar but NOT signal-date bar
        conn.execute(
            "INSERT INTO runs(asof_date, started_at, status) VALUES(?,datetime('now'),'success')",
            ("2026-01-02",),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO intents(run_id, symbol, action, strength, reason) VALUES(?,?,?,1.0,'test')",
            (run_id, "TSLA", "buy"),
        )
        intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO orders(symbol, side, qty, status, requested_at, intent_id) VALUES(?,?,?,?,datetime('now'),?)",
            ("TSLA", "buy", 5.0, "filled", intent_id),
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO executions(broker,broker_order_id,symbol,side,qty,filled_avg_price,filled_at,order_id) "
            "VALUES('alpaca-paper','BRK999','TSLA','buy',5.0,101.0,'2026-01-03T09:30:00Z',?)",
            (order_id,),
        )
        # NO bars_daily row for TSLA on 2026-01-02 (signal date)

    result = analyze_slippage(SlippageConfig())
    assert result["analyzed"] == 0
    assert result["skipped"].get("no_signal_bar", 0) == 1


# ── test 7: partial fills aggregated into weighted-avg ───────────────────────

def test_partial_fills_weighted_avg(db):
    from trading.db import connect
    with connect() as conn:
        signal_date = "2026-01-02"
        fill_date = "2026-01-03"
        symbol = "GOOG"

        conn.execute(
            "INSERT OR IGNORE INTO bars_daily(symbol,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
            (symbol, signal_date, 100.0, 100.0, 100.0, 100.0, 1_000_000),
        )
        conn.execute(
            "INSERT OR IGNORE INTO bars_daily(symbol,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
            (symbol, fill_date, 100.0, 105.0, 99.0, 102.0, 1_000_000),
        )
        conn.execute(
            "INSERT INTO runs(asof_date, started_at, status) VALUES(?,datetime('now'),'success')",
            (signal_date,),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO intents(run_id, symbol, action, strength, reason) VALUES(?,?,?,1.0,'test')",
            (run_id, symbol, "buy"),
        )
        intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO orders(symbol, side, qty, status, requested_at, intent_id) VALUES(?,?,?,?,datetime('now'),?)",
            (symbol, "buy", 15.0, "filled", intent_id),
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Two partial fills for the same order (distinct broker_order_ids)
        conn.executemany(
            "INSERT INTO executions(broker,broker_order_id,symbol,side,qty,filled_avg_price,filled_at,order_id) "
            "VALUES('alpaca-paper',?,?,?,?,?,?,?)",
            [
                ("BRK-FILL-1", symbol, "buy", 10.0, 101.0, f"{fill_date}T09:30:00Z", order_id),
                ("BRK-FILL-2", symbol, "buy",  5.0, 103.0, f"{fill_date}T10:00:00Z", order_id),
            ],
        )

    result = analyze_slippage(SlippageConfig())
    # Only one analyzed record (two fills merged into one)
    assert result["analyzed"] == 1
    row = result["rows"][0]
    assert row["qty"] == pytest.approx(15.0)
    # Weighted avg: (10*101 + 5*103) / 15 = (1010 + 515) / 15 = 1525/15 ≈ 101.667
    expected_fill = (10 * 101.0 + 5 * 103.0) / 15.0
    assert row["fill_price"] == pytest.approx(expected_fill, rel=1e-5)


# ── test 8: NULL filled_avg_price → skipped ───────────────────────────────────

def test_null_fill_price_skipped(db):
    from trading.db import connect
    with connect() as conn:
        signal_date = "2026-01-02"
        fill_date = "2026-01-03"
        symbol = "NVDA"

        conn.execute(
            "INSERT OR IGNORE INTO bars_daily(symbol,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
            (symbol, signal_date, 100.0, 100.0, 100.0, 100.0, 1_000_000),
        )
        conn.execute(
            "INSERT OR IGNORE INTO bars_daily(symbol,t,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
            (symbol, fill_date, 100.0, 102.0, 99.0, 101.0, 1_000_000),
        )
        conn.execute(
            "INSERT INTO runs(asof_date, started_at, status) VALUES(?,datetime('now'),'success')",
            (signal_date,),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO intents(run_id, symbol, action, strength, reason) VALUES(?,?,?,1.0,'test')",
            (run_id, symbol, "buy"),
        )
        intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO orders(symbol, side, qty, status, requested_at, intent_id) VALUES(?,?,?,?,datetime('now'),?)",
            (symbol, "buy", 5.0, "cancelled", intent_id),
        )
        order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # NULL filled_avg_price (cancelled order)
        conn.execute(
            "INSERT INTO executions(broker,broker_order_id,symbol,side,qty,filled_avg_price,filled_at,order_id) "
            "VALUES('alpaca-paper','BRK-CANCEL',?,?,?,NULL,?,?)",
            (symbol, "buy", 5.0, f"{fill_date}T09:30:00Z", order_id),
        )

    result = analyze_slippage(SlippageConfig())
    assert result["analyzed"] == 0
    assert result["skipped"].get("no_fill_price", 0) == 1
