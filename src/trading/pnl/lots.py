from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import logging

from ..db import connect

log = logging.getLogger("trading")


# ----------------------------
# Public return types
# ----------------------------

@dataclass
class LotsApplyResult:
    buys_processed: int = 0
    sells_processed: int = 0
    lots_created: int = 0
    closings_created: int = 0
    lots_updated: int = 0
    warnings: int = 0


# ----------------------------
# Helpers (row access)
# ----------------------------

def _row_get(r, key, default=None):
    try:
        # sqlite3.Row supports keys()
        if hasattr(r, "keys") and key in r.keys():
            return r[key]
        # plain dict
        if isinstance(r, dict):
            return r.get(key, default)
    except Exception:
        pass
    return default

def _pick(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """
    Return the first non-None value for the first existing key in `row`.

    IMPORTANT: sqlite3.Row implements `__contains__` as "value contains",
    not "column name contains". So we must check row.keys().
    """
    row_keys = None
    try:
        # sqlite3.Row has .keys()
        row_keys = set(row.keys())  # type: ignore[attr-defined]
    except Exception:
        row_keys = None

    for k in keys:
        has_key = (k in row_keys) if row_keys is not None else (k in row)
        if not has_key:
            continue
        try:
            v = row[k]  # works for sqlite3.Row and dict-like mappings
        except Exception:
            v = None
        if v is not None:
            return v

    return default

def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _to_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    return str(x)


# ----------------------------
# Core: idempotency checks
# ----------------------------

def _buy_execution_already_lotted(conn, exec_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM position_lots WHERE entry_execution_id = ? LIMIT 1;",
        (exec_id,),
    ).fetchone()
    return row is not None


def _sell_execution_already_closed(conn, exec_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM lot_closings WHERE exit_execution_id = ? LIMIT 1;",
        (exec_id,),
    ).fetchone()
    return row is not None


# ----------------------------
# Core: selecting executions
# ----------------------------

def _iter_candidate_executions(conn) -> Iterable[Mapping[str, Any]]:
    """
    Pull executions in chronological order.

    We intentionally keep this resilient to your exact schema by checking
    multiple possible column names for qty/price/time.
    """
    # We prefer filled_at/executed_at for ordering. Fall back to requested_at if needed.
    # If none exist, ordering by id still yields deterministic replay.
    cols = conn.execute("PRAGMA table_info(executions);").fetchall()
    names = {c["name"] for c in cols}

    time_col = None
    for cand in ("filled_at", "executed_at", "execution_time", "created_at", "requested_at"):
        if cand in names:
            time_col = cand
            break

    # Some schemas call side 'side' or 'action'
    side_col = "side" if "side" in names else ("action" if "action" in names else None)
    if side_col is None:
        raise RuntimeError("executions table must have a 'side' (or 'action') column")

    # symbol is required
    if "symbol" not in names:
        raise RuntimeError("executions table must have a 'symbol' column")

    # qty and price columns vary across implementations
    # We'll resolve later via _pick()

    order_by = f"ORDER BY {time_col} ASC, id ASC" if time_col else "ORDER BY id ASC"

    q = f"""
      SELECT *
      FROM executions
      {order_by};
    """
    rows = conn.execute(q).fetchall()
    for r in rows:
        yield r


# ----------------------------
# Core: lot operations
# ----------------------------

def _create_lot_for_buy(conn, ex: Mapping[str, Any]) -> int:
    exec_id = int(ex["id"])
    symbol = _to_str(ex["symbol"]).upper().strip()

    qty = _to_float(_pick(ex, "filled_qty", "qty", "quantity", default=0.0))
    price = _to_float(_pick(ex, "filled_avg_price", "avg_price", "price", "fill_price", default=0.0))
    filled_at = _to_str(_pick(ex, "filled_at", "executed_at", "execution_time", "created_at", default=""))

    if qty <= 0 or price <= 0:
        raise ValueError(f"Invalid BUY execution values: exec_id={exec_id} qty={qty} price={price}")

    cur = conn.execute(
        """
        INSERT INTO position_lots
          (symbol, qty_open, entry_price, entry_filled_at, entry_execution_id)
        VALUES
          (?, ?, ?, ?, ?);
        """,
        (symbol, qty, price, filled_at, exec_id),
    )
    return int(cur.lastrowid)


def _fifo_close_lots_for_sell(conn, ex: Mapping[str, Any], result: LotsApplyResult) -> None:
    exec_id = int(ex["id"])
    symbol = _to_str(ex["symbol"]).upper().strip()

    sell_qty = _to_float(_pick(ex, "filled_qty", "qty", "quantity", default=0.0))
    sell_price = _to_float(_pick(ex, "filled_avg_price", "avg_price", "price", "fill_price", default=0.0))
    filled_at = _to_str(_pick(ex, "filled_at", "executed_at", "execution_time", "created_at", default=""))

    if sell_qty <= 0 or sell_price <= 0:
        raise ValueError(f"Invalid SELL execution values: exec_id={exec_id} qty={sell_qty} price={sell_price}")

    remaining = sell_qty

    # Pull open lots in FIFO order
    lots = conn.execute(
        """
        SELECT id, qty_open, entry_price, entry_filled_at, entry_execution_id
        FROM position_lots
        WHERE symbol = ? AND qty_open > 0
        ORDER BY entry_filled_at ASC, id ASC;
        """,
        (symbol,),
    ).fetchall()

    if not lots:
        # This can happen if we started tracking after positions already existed,
        # or if executions history is incomplete.
        log.warning("SELL has no open lots to close (symbol=%s exec_id=%s qty=%s).", symbol, exec_id, sell_qty)
        result.warnings += 1
        return

    for lot in lots:
        if remaining <= 0:
            break

        lot_id = int(lot["id"])
        lot_qty_open = _to_float(lot["qty_open"])
        entry_price = _to_float(lot["entry_price"])
        entry_filled_at = _to_str(lot["entry_filled_at"])
        entry_exec_id = int(lot["entry_execution_id"])

        if lot_qty_open <= 0:
            continue

        chunk = min(remaining, lot_qty_open)
        realized = chunk * (sell_price - entry_price)

        # Insert closing record
        conn.execute(
            """
            INSERT INTO lot_closings
              (symbol, lot_id, qty_closed, entry_price, exit_price, realized_pnl,
               entry_filled_at, exit_filled_at, entry_execution_id, exit_execution_id)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                symbol,
                lot_id,
                chunk,
                entry_price,
                sell_price,
                realized,
                entry_filled_at,
                filled_at,
                entry_exec_id,
                exec_id,
            ),
        )
        result.closings_created += 1

        # Update lot qty_open
        new_open = lot_qty_open - chunk
        conn.execute(
            "UPDATE position_lots SET qty_open = ? WHERE id = ?;",
            (new_open, lot_id),
        )
        result.lots_updated += 1

        remaining -= chunk

    if remaining > 1e-9:
        # More sold than we can match from known lots (history incomplete / started mid-stream).
        log.warning(
            "SELL qty exceeds available open lots (symbol=%s exec_id=%s sold=%s unmatched=%s).",
            symbol,
            exec_id,
            sell_qty,
            remaining,
        )
        result.warnings += 1


# ----------------------------
# Public API
# ----------------------------

def apply_new_executions(run_id: int | None = None) -> LotsApplyResult:
    """
    Process any executions not yet reflected in position_lots / lot_closings.

    Idempotency:
      - BUY exec is processed iff position_lots.entry_execution_id exists
      - SELL exec is processed iff lot_closings.exit_execution_id exists

    Notes:
      - FIFO matching
      - Works with aggregated fills (1 execution per order) or true per-fill executions
    """
    res = LotsApplyResult()

    with connect() as conn:
        # Keep lot application consistent if called during/after run_once
        conn.execute("BEGIN;")
        try:
            for ex in _iter_candidate_executions(conn):
                side = _to_str(_pick(ex, "side", "action", default="")).lower().strip()
                if side not in ("buy", "sell"):
                    continue

                ok, qty, price, filled_at = _is_filled_execution(ex)
                if not ok:
                    # don’t crash runs because broker hasn’t finalized the fill yet
                    log.info(
                        "Skip execution (not filled yet): exec_id=%s side=%s sym=%s qty=%s price=%s filled_at=%s",
                        _row_get(ex, "id"), side, _row_get(ex, "symbol"), qty, price, filled_at
                    )
                    continue

                exec_id = int(ex["id"])

                if side == "buy":
                    if _buy_execution_already_lotted(conn, exec_id):
                        continue
                    lot_id = _create_lot_for_buy(conn, ex)
                    res.buys_processed += 1
                    res.lots_created += 1
                    log.debug("Lot created: lot_id=%s symbol=%s exec_id=%s", lot_id, ex["symbol"], exec_id)

                else:  # sell
                    if _sell_execution_already_closed(conn, exec_id):
                        continue
                    _fifo_close_lots_for_sell(conn, ex, res)
                    res.sells_processed += 1

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    if res.buys_processed or res.sells_processed:
        log.info(
            "Lots applied%s | buys=%s sells=%s lots=%s closings=%s warnings=%s",
            f" (run_id={run_id})" if run_id is not None else "",
            res.buys_processed,
            res.sells_processed,
            res.lots_created,
            res.closings_created,
            res.warnings,
        )

    return res


def rebuild_lots() -> LotsApplyResult:
    """
    One-time / debug command:
      - wipe position_lots and lot_closings
      - replay ALL executions in chronological order

    Use when:
      - you change lot logic
      - you suspect data drift
      - you want a clean backfill after turning Phase 3 on
    """
    res = LotsApplyResult()

    with connect() as conn:
        conn.execute("BEGIN;")
        try:
            conn.execute("DELETE FROM lot_closings;")
            conn.execute("DELETE FROM position_lots;")

            for ex in _iter_candidate_executions(conn):
                side = _to_str(_pick(ex, "side", "action", default="")).lower().strip()
                if side not in ("buy", "sell"):
                    continue
                exec_id = int(ex["id"])

                if side == "buy":
                    lot_id = _create_lot_for_buy(conn, ex)
                    res.buys_processed += 1
                    res.lots_created += 1
                    log.debug("Lot created (rebuild): lot_id=%s symbol=%s exec_id=%s", lot_id, ex["symbol"], exec_id)
                else:
                    _fifo_close_lots_for_sell(conn, ex, res)
                    res.sells_processed += 1

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    log.info(
        "Lots rebuild complete | buys=%s sells=%s lots=%s closings=%s warnings=%s",
        res.buys_processed,
        res.sells_processed,
        res.lots_created,
        res.closings_created,
        res.warnings,
    )
    return res


# ----------------------------
# Optional helpers for Phase 3 CLI
# ----------------------------

def get_open_lots_by_symbol() -> dict[str, list[dict[str, Any]]]:
    """
    Returns open lots grouped by symbol.
    Useful for `trading positions` command later.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, qty_open, entry_price, entry_filled_at, entry_execution_id
            FROM position_lots
            WHERE qty_open > 0
            ORDER BY symbol ASC, entry_filled_at ASC, id ASC;
            """
        ).fetchall()

    for r in rows:
        sym = _to_str(r["symbol"]).upper().strip()
        out.setdefault(sym, []).append(dict(r))
    return out


def get_realized_pnl_for_date(asof_date: str) -> float:
    """
    Realized P&L for a trading date based on lot_closings exit_filled_at.

    asof_date: 'YYYY-MM-DD'
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl), 0) AS pnl
            FROM lot_closings
            WHERE date(exit_filled_at) = date(?);
            """,
            (asof_date,),
        ).fetchone()
    return _to_float(row["pnl"] if row else 0.0)

def _is_filled_execution(ex: Mapping[str, Any]) -> tuple[bool, float, float, str]:
    qty = _to_float(_pick(ex, "filled_qty", "qty", "quantity", default=0.0))
    price = _to_float(_pick(ex, "filled_avg_price", "avg_price", "price", "fill_price", default=0.0))
    filled_at = _to_str(_pick(ex, "filled_at", "executed_at", "execution_time", default=""))
    ok = (qty > 0) and (price > 0) and bool(filled_at)
    return ok, qty, price, filled_at