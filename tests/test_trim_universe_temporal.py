from __future__ import annotations

import logging
import pytest

UNIV = "sp500"
RUN_ID = 1


@pytest.fixture
def bt(db, tmp_path, monkeypatch):
    """Live DB (`db`) + a fresh backtest DB seeded with a 2015→2025 run row."""
    bt_path = str(tmp_path / "backtest.sqlite")
    monkeypatch.setenv("BACKTEST_DB_PATH", bt_path)
    from trading.backtest.db import init_backtest_db, connect_backtest
    init_backtest_db(bt_path)
    with connect_backtest(bt_path) as c:
        c.execute(
            "INSERT INTO backtest_runs(id, strategy, start_date, end_date, universe, "
            "initial_capital, max_positions, per_position_notional) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (RUN_ID, "sma", "2015-01-01", "2025-12-31", UNIV, 10000.0, 5, 2000.0),
        )
        c.commit()
    return bt_path


def _add_to_universe(syms: list[str]) -> None:
    from trading.db import connect
    with connect() as conn:
        for s in syms:
            conn.execute(
                "INSERT OR IGNORE INTO universe_membership(universe, symbol, source) "
                "VALUES(?, ?, 'manual')",
                (UNIV, s),
            )


def _add_trades(bt_path: str, symbol: str, trades: list[tuple]) -> None:
    """trades: list of (exit_date 'YYYY-MM-DD', realized_pnl, return_pct)"""
    from trading.backtest.db import connect_backtest
    with connect_backtest(bt_path) as c:
        for ex_date, pnl, ret in trades:
            c.execute(
                "INSERT INTO backtest_trades(run_id, symbol, entry_date, exit_date, "
                "entry_price, exit_price, qty, realized_pnl, return_pct, exit_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (RUN_ID, symbol, ex_date, ex_date, 100.0, 100.0, 1.0, pnl, ret, "test"),
            )
        c.commit()


def _cfg(**overrides):
    from trading.universe.trim_universe import TrimConfig
    base = dict(backtest_run_id=RUN_ID, from_universe=UNIV)
    base.update(overrides)
    return TrimConfig(**base)


def _excl_rows():
    from trading.db import connect
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, reason_code, reason_note, reinstated_at "
            "FROM symbol_exclusions ORDER BY symbol"
        ).fetchall()
    return [dict(r) for r in rows]


def _membership_syms():
    from trading.db import connect
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol FROM universe_membership WHERE universe=?", (UNIV,)
        ).fetchall()
    return {r["symbol"] for r in rows}


# 9 small wins + 1 big loss → high concentration
_BLOWOUT_TRADES = [
    *[(f"{y}-06-01", 1.0, 1.0) for y in range(2015, 2024)],
    ("2024-06-01", -50.0, -50.0),
]

# 10 losses spread across 5 years → chronic
_CHRONIC_TRADES = [
    *[(f"{2015+i//2}-{(i%2)*5+1:02d}-15", -3.0, -3.0) for i in range(10)],
]


def test_single_year_blowout_is_kept(bt):
    _add_to_universe(["BLOW"])
    _add_trades(bt, "BLOW", _BLOWOUT_TRADES)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["excluded"] == 0
    assert len(summary["single_year_blowouts"]) == 1
    bo = summary["single_year_blowouts"][0]
    assert bo["symbol"] == "BLOW"
    assert bo["concentration_pct"] >= 70.0
    assert bo["year"] == "2024"
    assert _excl_rows() == []
    assert "BLOW" in _membership_syms()


def test_chronic_loser_is_excluded(bt):
    _add_to_universe(["BAD"])
    _add_trades(bt, "BAD", _CHRONIC_TRADES)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["chronic_losers"] == 1
    assert summary["consistent_losers"] == 0
    rows = _excl_rows()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BAD"
    assert rows[0]["reason_code"] == "chronic_loser"
    assert rows[0]["reason_note"].startswith("L2b:chronic")
    assert "run_id=1" in rows[0]["reason_note"]
    assert "period=2015→2025" in rows[0]["reason_note"]


def test_insufficient_entries_keeps_symbol(bt):
    _add_to_universe(["FEW"])
    # 5 trades over 5 years, all losing — entries < 8 → kept
    _add_trades(bt, "FEW", [(f"{2018+i}-06-01", -5.0, -5.0) for i in range(5)])

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["excluded"] == 0
    assert summary["chronic_losers"] == 0
    assert summary["passing"] == 1
    assert _excl_rows() == []


def test_too_few_years_keeps_symbol(bt):
    _add_to_universe(["SHORT"])
    # 8 trades across only 2 years, all losing
    trades = [(f"2023-{(i%6)+1:02d}-15", -3.0, -3.0) for i in range(4)]
    trades += [(f"2024-{(i%6)+1:02d}-15", -3.0, -3.0) for i in range(4)]
    _add_trades(bt, "SHORT", trades)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["excluded"] == 0
    assert summary["chronic_losers"] == 0
    assert summary["passing"] == 1
    assert _excl_rows() == []


def test_already_excluded_is_not_rewritten(bt):
    _add_to_universe(["DUPE"])
    _add_trades(bt, "DUPE", _CHRONIC_TRADES)  # would otherwise be chronic
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO symbol_exclusions(symbol, reason_code, reason_note, "
            "excluded_by, excluded_at) VALUES (?,?,?,?, datetime('now'))",
            ("DUPE", "manual", "operator note", "operator"),
        )

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["already_excluded_count"] == 1
    assert summary["excluded"] == 0
    rows = _excl_rows()
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "manual"
    assert rows[0]["reason_note"] == "operator note"


def test_previously_reinstated_kept_with_warning(bt, caplog):
    _add_to_universe(["REIN"])
    _add_trades(bt, "REIN", _CHRONIC_TRADES)
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO symbol_exclusions(symbol, reason_code, reason_note, "
            "excluded_by, excluded_at, reinstated_at, reinstated_note) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), ?)",
            ("REIN", "chronic_loser", "old note", "system", "operator override"),
        )

    from trading.universe.trim_universe import trim_universe
    with caplog.at_level(logging.WARNING, logger="trading"):
        summary = trim_universe(_cfg())

    assert summary["previously_reinstated_count"] == 1
    assert summary["excluded"] == 0
    rows = _excl_rows()
    assert len(rows) == 1
    assert rows[0]["reinstated_at"] is not None  # not rewritten
    assert summary["reinstated_warnings"] and summary["reinstated_warnings"][0][0] == "REIN"
    assert any("REIN" in m and "chronic" in m for m in caplog.messages)


def test_dry_run_writes_nothing(bt):
    _add_to_universe(["BAD"])
    _add_trades(bt, "BAD", _CHRONIC_TRADES)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg(dry_run=True))

    assert summary["dry_run"] is True
    assert summary["chronic_losers"] == 1
    assert _excl_rows() == []
    assert "BAD" in _membership_syms()


def test_remove_from_universe_deletes_rows(bt):
    _add_to_universe(["KILL", "KEEP"])
    _add_trades(bt, "KILL", _CHRONIC_TRADES)
    # KEEP has no trades; default (exclude_no_trades=False) keeps it

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg(remove_from_universe=True))

    assert summary["chronic_losers"] == 1
    assert summary["removed_from_universe"] == 1
    assert _membership_syms() == {"KEEP"}
    rows = _excl_rows()
    assert [r["symbol"] for r in rows] == ["KILL"]


def test_default_keeps_universe_unchanged(bt):
    _add_to_universe(["BAD"])
    _add_trades(bt, "BAD", _CHRONIC_TRADES)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())  # remove_from_universe=False

    assert summary["chronic_losers"] == 1
    assert summary["removed_from_universe"] == 0
    assert "BAD" in _membership_syms()
    assert [r["symbol"] for r in _excl_rows()] == ["BAD"]


# --- concentration_pct fix: years_negative gate + total-loss denominator ---


def test_concentration_one_losing_year_offset_by_positive_years(bt):
    """+$10/-$50/+$20/+$15 over 4 years (8 trades, net -$5).
    total_loss=$50, concentration=100%, years_neg=1 → single_year_blowout."""
    _add_to_universe(["OFFSET"])
    trades = [
        # 2015 — 2 wins of +$5 each (+$10)
        ("2015-03-15",  5.0,  5.0),
        ("2015-09-15",  5.0,  5.0),
        # 2016 — 2 losses of -$25 each (-$50)
        ("2016-03-15", -25.0, -25.0),
        ("2016-09-15", -25.0, -25.0),
        # 2017 — 2 wins of +$10 each (+$20)
        ("2017-03-15", 10.0, 10.0),
        ("2017-09-15", 10.0, 10.0),
        # 2018 — 2 wins of +$7.50 each (+$15)
        ("2018-03-15",  7.5,  7.5),
        ("2018-09-15",  7.5,  7.5),
    ]
    _add_trades(bt, "OFFSET", trades)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["excluded"] == 0
    assert len(summary["single_year_blowouts"]) == 1
    bo = summary["single_year_blowouts"][0]
    assert bo["symbol"] == "OFFSET"
    # 100*$50/$50 = 100% (bounded)
    assert 99.0 <= bo["concentration_pct"] <= 100.0
    assert bo["year"] == "2016"
    assert "OFFSET" in _membership_syms()


def test_three_roughly_equal_losing_years_is_chronic(bt):
    """-$10/-$15/-$12 over 3 years (9 trades, all losers).
    total_loss=$37, concentration=40.5%, years_neg=3 → chronic_loser."""
    _add_to_universe(["EQLOSS"])
    trades = (
        [(f"2015-{i*3+1:02d}-15", -10.0 / 3, -10.0 / 3) for i in range(3)]
        + [(f"2016-{i*3+1:02d}-15", -5.0, -5.0) for i in range(3)]
        + [(f"2017-{i*3+1:02d}-15", -4.0, -4.0) for i in range(3)]
    )
    _add_trades(bt, "EQLOSS", trades)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["chronic_losers"] == 1
    assert summary["single_year_blowouts"] == []
    rows = _excl_rows()
    assert rows[0]["symbol"] == "EQLOSS"
    assert rows[0]["reason_code"] == "chronic_loser"
    # 100*$15/$37 ≈ 40-41%
    note = rows[0]["reason_note"]
    assert "conc=41%" in note or "conc=40%" in note


def test_one_big_plus_two_small_losing_years_is_chronic_not_blowout(bt):
    """-$30/-$3/-$2 over 3 years (8 trades, all losers).
    total_loss=$35, concentration=85.7%, years_neg=3 → falls through to
    chronic (NOT blowout, because years_negative > 2)."""
    _add_to_universe(["MIXED"])
    trades = (
        [(f"2015-{i+1:02d}-15", -5.0, -5.0) for i in range(6)]   # -$30
        + [("2016-06-15", -3.0, -3.0)]                            # -$3
        + [("2017-06-15", -2.0, -2.0)]                            # -$2
    )
    _add_trades(bt, "MIXED", trades)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    # NOT classified as single_year_blowout despite conc≈86%
    assert summary["single_year_blowouts"] == []
    assert summary["chronic_losers"] == 1
    rows = _excl_rows()
    assert rows[0]["symbol"] == "MIXED"
    assert rows[0]["reason_code"] == "chronic_loser"
    # Concentration in the note is bounded ≤100% (≈86%)
    assert "conc=86%" in rows[0]["reason_note"]


def test_profitable_symbol_with_one_losing_year_is_kept(bt):
    """+$40/-$20 over 2 years (8 trades, net +$20). Profitable: never a
    blowout (net>=0 gate), never chronic (net>=0 gate). Just kept."""
    _add_to_universe(["PROFIT"])
    trades = (
        [(f"2015-{i+1:02d}-15", 10.0, 10.0) for i in range(4)]   # +$40
        + [(f"2016-{i+1:02d}-15", -5.0, -5.0) for i in range(4)]  # -$20
    )
    _add_trades(bt, "PROFIT", trades)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    assert summary["excluded"] == 0
    assert summary["single_year_blowouts"] == []
    assert summary["passing"] == 1
    assert "PROFIT" in _membership_syms()
    assert _excl_rows() == []


def test_concentration_pct_never_exceeds_100(bt):
    """Sanity check: with offsetting positive years, concentration_pct
    must stay bounded in [0, 100]. The old formula produced >100%."""
    _add_to_universe(["CHK"])
    # +$100/-$50, net +$50: concentration = $50/$50 = 100%
    trades = [
        ("2015-06-01", 100.0, 100.0),
        ("2016-06-01", -50.0, -50.0),
    ]
    _add_trades(bt, "CHK", trades)

    from trading.universe.trim_universe import trim_universe
    summary = trim_universe(_cfg())

    chk = next(d for d in summary["decisions"] if d.symbol == "CHK")
    assert 0.0 <= chk.concentration_pct <= 100.0
    assert chk.concentration_pct == 100.0
