from __future__ import annotations

import logging
import pytest

from tests.conftest import make_dates

ASOF = "2024-05-01"
UNIV = "testu"


def _insert_membership(syms: list[str], universe: str = UNIV) -> None:
    from trading.db import connect
    with connect() as conn:
        for s in syms:
            conn.execute(
                "INSERT OR IGNORE INTO universe_membership(universe, symbol, source) "
                "VALUES(?, ?, 'manual')",
                (universe, s),
            )


def _insert_bars_ohlcv(symbol: str, bars: list[tuple], end: str = ASOF) -> None:
    """bars: list of (o, h, l, c, v) tuples."""
    from trading.db import connect
    dates = make_dates(len(bars), end)
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO bars_daily(symbol, t, o, h, l, c, v) "
            "VALUES (?,?,?,?,?,?,?);",
            [(symbol, d, o, h, l, c, v) for d, (o, h, l, c, v) in zip(dates, bars)],
        )


def _trend_bars(n: int = 300, start: float = 100.0, step: float = 0.10,
                vol: float = 20_000.0, gap_indices: list[int] | None = None) -> list[tuple]:
    """Slow uptrend so close > SMA200 throughout the last 252 days.
    `gap_indices` (into the returned list) inject opens at 0.90 * prev_close."""
    closes = [start + i * step for i in range(n)]
    bars: list[tuple] = []
    for i in range(n):
        c = closes[i]
        prev_c = closes[i - 1] if i > 0 else c
        o = prev_c
        if gap_indices and i in set(gap_indices):
            o = prev_c * 0.90
        h = max(o, c)
        l = min(o, c)
        bars.append((o, h, l, c, vol))
    return bars


def _flat_low_volume_bars(n: int = 300) -> list[tuple]:
    """Flat price with tiny volume → adv20 well below the 1M floor."""
    return [(100.0, 100.0, 100.0, 100.0, 100.0)] * n


def _audit_cfg(**overrides):
    from trading.universe.universe_builder import BuilderConfig
    base = dict(
        audit_mode=True,
        audit_universe=UNIV,
        min_adv20=1_000_000.0,
        min_price=10.0,
        min_pct_above_sma200=0.55,
        max_gap_down_freq=0.03,
        min_rsi2_recovery_rate=0.55,
        lookback_days=756,
    )
    base.update(overrides)
    return BuilderConfig(**base)


def _excl_rows(reinstated: bool | None = None) -> list[dict]:
    """reinstated=None → all rows; True/False filters by reinstated_at presence."""
    from trading.db import connect
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, reason_code, reason_note, excluded_by, reinstated_at "
            "FROM symbol_exclusions ORDER BY symbol"
        ).fetchall()
    out = [dict(r) for r in rows]
    if reinstated is True:
        out = [r for r in out if r["reinstated_at"] is not None]
    elif reinstated is False:
        out = [r for r in out if r["reinstated_at"] is None]
    return out


def _membership_syms() -> set[str]:
    from trading.db import connect
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol FROM universe_membership WHERE universe=?", (UNIV,)
        ).fetchall()
    return {r["symbol"] for r in rows}


def test_layer_1_adv20_too_low_writes_data_anomaly(db):
    _insert_membership(["LOWADV"])
    _insert_bars_ohlcv("LOWADV", _flat_low_volume_bars(300))

    from trading.universe.universe_builder import audit_existing_universe
    summary = audit_existing_universe(_audit_cfg())

    assert summary["layer_1_failures"]["count"] == 1
    assert summary["layer_2a_failures"]["count"] == 0
    rows = _excl_rows(reinstated=False)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "LOWADV"
    assert rows[0]["reason_code"] == "data_anomaly"
    assert rows[0]["reason_note"].startswith("L1:adv20<")
    assert rows[0]["excluded_by"] == "system"


def test_layer_2a_gap_down_freq_writes_chronic_loser(db):
    _insert_membership(["GAPPY"])
    # 252 days within the lookback; 10 gap-down events → freq ~0.04 > 0.03.
    # Place gaps at indices in the last 252 bars (n=300, so indices 48..299).
    gap_idx = [60, 80, 100, 120, 140, 160, 180, 200, 220, 240]
    _insert_bars_ohlcv("GAPPY", _trend_bars(n=300, gap_indices=gap_idx))

    from trading.universe.universe_builder import audit_existing_universe
    summary = audit_existing_universe(_audit_cfg())

    assert summary["layer_2a_failures"]["count"] == 1
    rows = _excl_rows(reinstated=False)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "GAPPY"
    assert rows[0]["reason_code"] == "chronic_loser"
    assert rows[0]["reason_note"].startswith("L2a:gap_down_freq=")


def test_rsi2_no_events_is_kept_not_excluded(db, caplog):
    _insert_membership(["NOEVENT"])
    # Monotonic uptrend with high volume → passes liquidity, trend, gap_down;
    # RSI2 never dips ≤ 10 in a smooth uptrend → rsi2_no_events.
    _insert_bars_ohlcv("NOEVENT", _trend_bars(n=300))

    from trading.universe.universe_builder import audit_existing_universe
    with caplog.at_level(logging.INFO, logger="trading"):
        summary = audit_existing_universe(_audit_cfg())

    assert summary["layer_1_failures"]["count"] == 0
    assert summary["layer_2a_failures"]["count"] == 0
    assert summary["rsi2_no_events_kept_count"] == 1
    assert _excl_rows() == []
    # Symbol remains in the universe
    assert "NOEVENT" in _membership_syms()


def test_already_excluded_is_not_rewritten(db):
    _insert_membership(["DUPE"])
    _insert_bars_ohlcv("DUPE", _flat_low_volume_bars(300))
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO symbol_exclusions(symbol, reason_code, reason_note, "
            "excluded_by, excluded_at) VALUES (?,?,?,?, datetime('now'))",
            ("DUPE", "manual", "operator note", "operator"),
        )

    from trading.universe.universe_builder import audit_existing_universe
    summary = audit_existing_universe(_audit_cfg())

    assert summary["already_excluded_count"] == 1
    assert summary["layer_1_failures"]["count"] == 0
    rows = _excl_rows()
    assert len(rows) == 1
    # Existing row preserved — reason_code/note NOT overwritten.
    assert rows[0]["reason_code"] == "manual"
    assert rows[0]["reason_note"] == "operator note"
    assert rows[0]["excluded_by"] == "operator"


def test_previously_reinstated_kept_with_warning(db, caplog):
    _insert_membership(["REIN"])
    _insert_bars_ohlcv("REIN", _flat_low_volume_bars(300))  # would fail L1 adv20
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO symbol_exclusions(symbol, reason_code, reason_note, "
            "excluded_by, excluded_at, reinstated_at, reinstated_note) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), ?)",
            ("REIN", "data_anomaly", "L1:adv20<...", "system", "operator override"),
        )

    from trading.universe.universe_builder import audit_existing_universe
    with caplog.at_level(logging.WARNING, logger="trading"):
        summary = audit_existing_universe(_audit_cfg())

    assert summary["previously_reinstated_count"] == 1
    assert summary["layer_1_failures"]["count"] == 0
    rows = _excl_rows()
    assert len(rows) == 1
    assert rows[0]["reinstated_at"] is not None  # untouched, still reinstated
    assert summary["reinstated_warnings"] and summary["reinstated_warnings"][0][0] == "REIN"
    assert any("REIN" in m for m in caplog.messages)


def test_dry_run_writes_nothing(db):
    _insert_membership(["DRY"])
    _insert_bars_ohlcv("DRY", _flat_low_volume_bars(300))

    from trading.universe.universe_builder import audit_existing_universe
    summary = audit_existing_universe(_audit_cfg(dry_run=True))

    assert summary["dry_run"] is True
    assert summary["layer_1_failures"]["count"] == 1
    assert _excl_rows() == []
    assert "DRY" in _membership_syms()


def test_remove_from_universe_deletes_failing_rows(db):
    _insert_membership(["KILL", "KEEP"])
    _insert_bars_ohlcv("KILL", _flat_low_volume_bars(300))     # fails L1 adv20
    _insert_bars_ohlcv("KEEP", _trend_bars(n=300))             # rsi2_no_events_kept

    from trading.universe.universe_builder import audit_existing_universe
    summary = audit_existing_universe(_audit_cfg(remove_from_universe=True))

    assert summary["removed_from_universe"] == 1
    rows = _excl_rows()
    assert {r["symbol"] for r in rows} == {"KILL"}
    assert _membership_syms() == {"KEEP"}


def test_default_keeps_universe_unchanged(db):
    _insert_membership(["FAIL"])
    _insert_bars_ohlcv("FAIL", _flat_low_volume_bars(300))

    from trading.universe.universe_builder import audit_existing_universe
    summary = audit_existing_universe(_audit_cfg())  # remove_from_universe=False

    assert summary["removed_from_universe"] == 0
    assert {r["symbol"] for r in _excl_rows()} == {"FAIL"}
    assert "FAIL" in _membership_syms()


def test_empty_universe_raises(db):
    from trading.universe.universe_builder import audit_existing_universe
    with pytest.raises(RuntimeError, match="empty"):
        audit_existing_universe(_audit_cfg())


# --- for_audit() defaults & disabled-filter behavior ----------------------

def _flat_high_volume_bars(n: int = 300, price: float = 100.0,
                           vol: float = 30_000.0) -> list[tuple]:
    """Flat price → SMA200 == price → pct_above_sma200 = 0; rsi2 has no events."""
    return [(price, price, price, price, vol)] * n


def test_for_audit_defaults():
    """for_audit() disables pct_above_sma200 and loosens gap_down_freq."""
    from trading.universe.universe_builder import BuilderConfig
    cfg = BuilderConfig.for_audit()
    assert cfg.min_pct_above_sma200 == 0.0
    assert cfg.max_gap_down_freq == 0.05
    assert cfg.min_rsi2_recovery_rate == 0.55
    assert cfg.min_adv20 == 1_000_000.0
    assert cfg.min_price == 10.0


def test_build_mode_defaults_unchanged():
    """Plain BuilderConfig() keeps build-mode defaults intact."""
    from trading.universe.universe_builder import BuilderConfig
    cfg = BuilderConfig()
    assert cfg.min_pct_above_sma200 == 0.55
    assert cfg.max_gap_down_freq == 0.03
    assert cfg.min_rsi2_recovery_rate == 0.55
    assert cfg.min_adv20 == 1_000_000.0
    assert cfg.min_price == 10.0


def test_for_audit_kwargs_override():
    """Explicit kwargs override for_audit() defaults."""
    from trading.universe.universe_builder import BuilderConfig
    cfg = BuilderConfig.for_audit(min_pct_above_sma200=0.55, min_adv20=2_000_000.0)
    assert cfg.min_pct_above_sma200 == 0.55
    assert cfg.min_adv20 == 2_000_000.0
    # Other audit defaults still apply
    assert cfg.max_gap_down_freq == 0.05


def test_for_audit_pct_sma200_disabled_keeps_flat_symbol(db):
    """With for_audit() defaults a flat stock (pct_above_sma200=0) is KEPT
    — pct check is disabled, gap freq is 0, and rsi2 has no events."""
    _insert_membership(["FLAT"])
    _insert_bars_ohlcv("FLAT", _flat_high_volume_bars(300))

    from trading.universe.universe_builder import (
        audit_existing_universe, BuilderConfig,
    )
    cfg = BuilderConfig.for_audit(audit_mode=True, audit_universe=UNIV)
    summary = audit_existing_universe(cfg)

    assert summary["layer_1_failures"]["count"] == 0
    assert summary["layer_2a_failures"]["count"] == 0
    assert _excl_rows() == []
    assert "FLAT" in _membership_syms()


def test_explicit_min_pct_above_sma200_flags_failure(db):
    """When the operator overrides --min-trend-pct to a build-mode value
    the same flat stock fails L2a:pct_above_sma200."""
    _insert_membership(["FLAT"])
    _insert_bars_ohlcv("FLAT", _flat_high_volume_bars(300))

    from trading.universe.universe_builder import (
        audit_existing_universe, BuilderConfig,
    )
    cfg = BuilderConfig.for_audit(
        audit_mode=True, audit_universe=UNIV,
        min_pct_above_sma200=0.55,
    )
    summary = audit_existing_universe(cfg)

    assert summary["layer_2a_failures"]["count"] == 1
    rows = _excl_rows(reinstated=False)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "FLAT"
    assert rows[0]["reason_code"] == "chronic_loser"
    assert rows[0]["reason_note"].startswith("L2a:pct_above_sma200=")


def _audit_cli_args(**overrides):
    """Build an argparse.Namespace matching audit-universe defaults."""
    import argparse
    base = dict(
        universe=UNIV,
        source="existing",
        min_adv20=1_000_000.0,
        min_price=10.0,
        min_trend_pct=0.0,       # audit default
        max_gap_freq=0.05,       # audit default
        min_rsi2_rate=0.55,
        lookback_days=756,
        exclude_symbols="",
        remove_from_universe=False,
        dry_run=True,
        report=False,
        use_backtest_db=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_header_shows_disabled_for_pct_sma200(db, capsys):
    """CLI default audit run prints 'DISABLED' for pct_above_sma200 and
    notes the gap_down_freq filter is 'structural only'."""
    _insert_membership(["NOEVENT"])
    _insert_bars_ohlcv("NOEVENT", _trend_bars(n=300))

    from trading.cli import cmd_audit_universe
    cmd_audit_universe(_audit_cli_args())
    out = capsys.readouterr().out

    assert "Filters active:" in out
    assert "pct_above_sma200:     DISABLED" in out
    assert "regime-sensitive" in out
    assert "gap_down_freq <= 0.05 (structural only)" in out
    assert "rsi2_recovery >= 0.55 (3yr behavioral)" in out


def test_cli_header_no_disabled_when_pct_explicit(db, capsys):
    """When the operator explicitly raises --min-trend-pct, the header
    shows the numeric threshold instead of DISABLED."""
    _insert_membership(["NOEVENT"])
    _insert_bars_ohlcv("NOEVENT", _trend_bars(n=300))

    from trading.cli import cmd_audit_universe
    cmd_audit_universe(_audit_cli_args(min_trend_pct=0.55))
    out = capsys.readouterr().out

    assert "pct_above_sma200 >= 0.55" in out
    assert "pct_above_sma200:     DISABLED" not in out


def test_cli_layer2a_section_notes_pct_disabled(db, capsys):
    """When pct_above_sma200 is disabled but gap_down_freq still fires,
    the Layer 2a section explicitly notes pct_above_sma200 was skipped."""
    _insert_membership(["GAPPY"])
    gap_idx = [60, 80, 100, 120, 140, 160, 180, 200, 220, 240]
    _insert_bars_ohlcv("GAPPY", _trend_bars(n=300, gap_indices=gap_idx))

    from trading.cli import cmd_audit_universe
    cmd_audit_universe(_audit_cli_args())  # max_gap_freq=0.05 still flags ~0.04
    out = capsys.readouterr().out

    # The Layer 2a header should be present and explain the disabled state.
    assert "Layer 2a failures (chronic_loser):" in out
    assert "pct_above_sma200 disabled" in out
