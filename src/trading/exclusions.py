from __future__ import annotations

import logging
import os
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

def get_active_exclusions() -> set[str]:
    """
    Return set of currently excluded symbols (reinstated_at IS NULL).
    Also includes symbols in TRADING_EXCLUDE_SYMBOLS env var for backward compat.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol FROM symbol_exclusions WHERE reinstated_at IS NULL"
        ).fetchall()
    db_excluded = {r["symbol"] for r in rows}

    env_str = os.getenv("TRADING_EXCLUDE_SYMBOLS", "").strip()
    env_excluded = {s.strip().upper() for s in env_str.split(",") if s.strip()}

    return db_excluded | env_excluded


def add_exclusion(
    symbol: str,
    reason_code: str,
    reason_note: str = "",
    excluded_by: str = "operator",
    review_after: Optional[str] = None,
) -> bool:
    """
    Add a symbol to exclusions. If already excluded, updates reason if changed.
    Returns True if newly added, False if already existed.
    """
    symbol = symbol.strip().upper()
    if reason_code not in VALID_REASON_CODES:
        raise ValueError(
            f"Invalid reason_code '{reason_code}'. "
            f"Valid: {sorted(VALID_REASON_CODES)}"
        )
    with connect() as conn:
        existing = conn.execute(
            "SELECT symbol, reinstated_at FROM symbol_exclusions WHERE symbol=?",
            (symbol,),
        ).fetchone()

        if existing is None:
            conn.execute(
                """INSERT INTO symbol_exclusions
                   (symbol, reason_code, reason_note, excluded_by, review_after)
                   VALUES (?, ?, ?, ?, ?)""",
                (symbol, reason_code, reason_note, excluded_by, review_after),
            )
            log.info("Excluded %s reason=%s", symbol, reason_code)
            return True
        elif existing["reinstated_at"] is not None:
            conn.execute(
                """UPDATE symbol_exclusions SET
                   reason_code=?, reason_note=?, excluded_by=?,
                   excluded_at=datetime('now'), review_after=?,
                   reinstated_at=NULL, reinstated_note=NULL
                   WHERE symbol=?""",
                (reason_code, reason_note, excluded_by, review_after, symbol),
            )
            log.info("Re-excluded %s reason=%s", symbol, reason_code)
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
) -> list[dict]:
    """
    Return list of exclusion records as dicts.
    reason_code: filter by specific code (None = all)
    include_reinstated: include previously excluded but now active symbols
    review_due: only return symbols whose review_after date has passed
    """
    conditions = []
    params: list = []

    if not include_reinstated:
        conditions.append("reinstated_at IS NULL")
    if reason_code:
        conditions.append("reason_code = ?")
        params.append(reason_code)
    if review_due:
        conditions.append("review_after IS NOT NULL AND review_after <= date('now')")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with connect() as conn:
        rows = conn.execute(
            f"""SELECT symbol, reason_code, reason_note, excluded_by,
                       excluded_at, review_after, reinstated_at, reinstated_note
                FROM symbol_exclusions {where}
                ORDER BY excluded_at DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def migrate_from_env(dry_run: bool = False) -> dict:
    """
    One-time migration: read TRADING_EXCLUDE_SYMBOLS from env and
    write each symbol to symbol_exclusions table.
    Skips symbols already in the table.
    """
    env_str = os.getenv("TRADING_EXCLUDE_SYMBOLS", "").strip()
    if not env_str:
        return {"migrated": 0, "skipped": 0, "symbols": []}

    symbols = [s.strip().upper() for s in env_str.split(",") if s.strip()]
    migrated = []
    skipped = []

    for sym in symbols:
        with connect() as conn:
            exists = conn.execute(
                "SELECT symbol FROM symbol_exclusions WHERE symbol=?", (sym,)
            ).fetchone()
        if exists:
            skipped.append(sym)
        elif not dry_run:
            add_exclusion(
                symbol=sym,
                reason_code="chronic_loser",
                reason_note="Migrated from TRADING_EXCLUDE_SYMBOLS env var",
                excluded_by="system",
            )
            migrated.append(sym)
        else:
            migrated.append(sym)

    return {"migrated": len(migrated), "skipped": len(skipped), "symbols": migrated}
