from __future__ import annotations

import logging
from typing import Optional

from .db import connect

log = logging.getLogger("trading")

VALID_REASON_CODES = {
    "chronic_loser",
    "sp500_removal",
    "data_anomaly",
    "manual",
    "gap_risk",
    "earnings_risk",
}

VALID_STRATEGY_SCOPES = {"all", "swing", "mrit", "portfolio"}

# Which strategy_scope values apply to each strategy.
# None means no scope filter — return all active exclusions.
_SCOPE_RESOLUTION: dict[str, tuple[str, ...] | None] = {
    "mrit":      ("all", "swing", "mrit"),   # MRIT sees: global + swing + mrit-specific
    "sma":       ("all", "swing"),            # SMA sees:  global + swing
    "portfolio": ("all", "portfolio"),        # Portfolio sees: global + portfolio-specific
    "all":       None,                        # No filter — union of all scopes
}


def get_active_exclusions(strategy: str = "mrit") -> set[str]:
    """
    Return set of currently excluded symbols for the given strategy.

    strategy values:
      "mrit"      → scopes: all, swing, mrit   (default)
      "sma"       → scopes: all, swing
      "portfolio" → scopes: all, portfolio
      "all"       → all active exclusions regardless of scope
    """
    if strategy not in _SCOPE_RESOLUTION:
        raise ValueError(
            f"Invalid strategy '{strategy}'. "
            f"Valid: {sorted(_SCOPE_RESOLUTION)}"
        )
    scopes = _SCOPE_RESOLUTION[strategy]
    with connect() as conn:
        if scopes is None:
            rows = conn.execute(
                "SELECT symbol FROM symbol_exclusions WHERE reinstated_at IS NULL"
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(scopes))
            rows = conn.execute(
                f"SELECT symbol FROM symbol_exclusions "
                f"WHERE reinstated_at IS NULL "
                f"AND strategy_scope IN ({placeholders})",
                list(scopes),
            ).fetchall()
    return {r["symbol"] for r in rows}


def add_exclusion(
    symbol: str,
    reason_code: str,
    reason_note: str = "",
    excluded_by: str = "operator",
    review_after: Optional[str] = None,
    strategy_scope: str = "all",
) -> bool:
    """
    Add a symbol to exclusions. If already excluded, updates reason if changed.
    Returns True if newly added/re-excluded, False if already excluded.

    strategy_scope: 'all' | 'swing' | 'mrit' | 'portfolio'
    Note: data_anomaly must always use scope='all' (enforced).
    """
    symbol = symbol.strip().upper()
    if reason_code not in VALID_REASON_CODES:
        raise ValueError(
            f"Invalid reason_code '{reason_code}'. "
            f"Valid: {sorted(VALID_REASON_CODES)}"
        )
    if strategy_scope not in VALID_STRATEGY_SCOPES:
        raise ValueError(
            f"Invalid strategy_scope '{strategy_scope}'. "
            f"Valid: {sorted(VALID_STRATEGY_SCOPES)}"
        )
    if reason_code == "data_anomaly" and strategy_scope != "all":
        raise ValueError(
            "data_anomaly exclusions must always use strategy_scope='all' "
            "(they are objective data-quality filters that apply to every strategy)."
        )
    with connect() as conn:
        existing = conn.execute(
            "SELECT symbol, reinstated_at FROM symbol_exclusions WHERE symbol=?",
            (symbol,),
        ).fetchone()

        if existing is None:
            conn.execute(
                """INSERT INTO symbol_exclusions
                   (symbol, reason_code, reason_note, excluded_by, review_after,
                    strategy_scope)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (symbol, reason_code, reason_note, excluded_by, review_after,
                 strategy_scope),
            )
            log.info("Excluded %s reason=%s scope=%s", symbol, reason_code, strategy_scope)
            return True
        elif existing["reinstated_at"] is not None:
            conn.execute(
                """UPDATE symbol_exclusions SET
                   reason_code=?, reason_note=?, excluded_by=?,
                   excluded_at=datetime('now'), review_after=?,
                   reinstated_at=NULL, reinstated_note=NULL,
                   strategy_scope=?
                   WHERE symbol=?""",
                (reason_code, reason_note, excluded_by, review_after,
                 strategy_scope, symbol),
            )
            log.info("Re-excluded %s reason=%s scope=%s", symbol, reason_code, strategy_scope)
            return True
        else:
            if reason_note:
                conn.execute(
                    "UPDATE symbol_exclusions SET reason_note=? WHERE symbol=?",
                    (reason_note, symbol),
                )
            log.info("Already excluded %s", symbol)
            return False


def reinstate_symbol(symbol: str, note: str = "") -> bool:
    """
    Mark a symbol as reinstated (no longer excluded).
    Returns True if reinstated, False if symbol not found or already active.
    """
    symbol = symbol.strip().upper()
    with connect() as conn:
        row = conn.execute(
            "SELECT symbol, reinstated_at FROM symbol_exclusions WHERE symbol=?",
            (symbol,),
        ).fetchone()
        if not row:
            return False
        if row["reinstated_at"] is not None:
            return False
        conn.execute(
            """UPDATE symbol_exclusions
               SET reinstated_at=datetime('now'), reinstated_note=?
               WHERE symbol=?""",
            (note, symbol),
        )
    log.info("Reinstated %s note=%s", symbol, note)
    return True


def list_exclusions(
    reason_code: Optional[str] = None,
    include_reinstated: bool = False,
    review_due: bool = False,
    strategy_scope: Optional[str] = None,
    strategy: Optional[str] = None,
) -> list[dict]:
    """
    Return list of exclusion records as dicts.

    reason_code:        filter by specific code (None = all)
    include_reinstated: include previously excluded but now active symbols
    review_due:         only return symbols whose review_after date has passed
    strategy_scope:     filter by literal scope value ('all','swing','mrit','portfolio')
    strategy:           filter by strategy using scope resolution — shows all rows that
                        would be excluded for that strategy. Mutually exclusive with
                        strategy_scope.
    """
    if strategy_scope is not None and strategy is not None:
        raise ValueError("Pass either strategy_scope or strategy, not both.")
    if strategy_scope is not None and strategy_scope not in VALID_STRATEGY_SCOPES:
        raise ValueError(
            f"Invalid strategy_scope '{strategy_scope}'. "
            f"Valid: {sorted(VALID_STRATEGY_SCOPES)}"
        )
    if strategy is not None and strategy not in _SCOPE_RESOLUTION:
        raise ValueError(
            f"Invalid strategy '{strategy}'. "
            f"Valid: {sorted(_SCOPE_RESOLUTION)}"
        )

    conditions: list[str] = []
    params: list = []

    if not include_reinstated:
        conditions.append("reinstated_at IS NULL")
    if reason_code:
        conditions.append("reason_code = ?")
        params.append(reason_code)
    if review_due:
        conditions.append("review_after IS NOT NULL AND review_after <= date('now')")
    if strategy_scope is not None:
        conditions.append("strategy_scope = ?")
        params.append(strategy_scope)
    if strategy is not None:
        scopes = _SCOPE_RESOLUTION[strategy]
        if scopes is not None:
            placeholders = ",".join("?" * len(scopes))
            conditions.append(f"strategy_scope IN ({placeholders})")
            params.extend(list(scopes))
        # strategy="all" → scopes is None → no scope filter

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with connect() as conn:
        rows = conn.execute(
            f"""SELECT symbol, reason_code, reason_note, excluded_by,
                       excluded_at, review_after, reinstated_at, reinstated_note,
                       strategy_scope
                FROM symbol_exclusions {where}
                ORDER BY excluded_at DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
