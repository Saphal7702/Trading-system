from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SimPosition:
    symbol: str
    qty: float
    entry_price: float           # avg cost basis — mutates on pyramid adds
    entry_date: str
    peak_price: float
    entry_notional: float        # total cost — sums on pyramid adds
    signal_key: str = ""
    # Frozen at the initial fill; unchanged by subsequent pyramid adds.
    # For non-pyramid positions these equal entry_price / entry_notional throughout.
    entry_notional_original: float = 0.0
    original_entry_price: float = 0.0
    pyramid_rungs_hit: frozenset = frozenset()  # indices of rungs already filled


class SimBroker:
    def __init__(self, cash: float) -> None:
        self.cash = float(cash)
        self.positions: dict[str, SimPosition] = {}

    def fill_buy(
        self,
        symbol: str,
        price: float,
        notional: float,
        date: str,
        signal_key: str = "",
        *,
        _pyramid_rung: int | None = None,
    ) -> float:
        """Buy at price with given notional. Returns qty filled (0 if insufficient cash).

        _pyramid_rung: when set, this is a pyramid add — the rung index is recorded in
        pyramid_rungs_hit and entry_notional_original / original_entry_price are preserved.
        """
        if price <= 0 or notional <= 0 or self.cash <= 0:
            return 0.0
        affordable = min(notional, self.cash)
        qty = affordable / price
        cost = qty * price
        self.cash -= cost
        if symbol in self.positions:
            pos = self.positions[symbol]
            total_qty = pos.qty + qty
            new_avg = (pos.entry_price * pos.qty + price * qty) / total_qty
            new_rungs = (
                frozenset(pos.pyramid_rungs_hit | {_pyramid_rung})
                if _pyramid_rung is not None
                else pos.pyramid_rungs_hit
            )
            self.positions[symbol] = SimPosition(
                symbol=symbol,
                qty=total_qty,
                entry_price=new_avg,
                entry_date=pos.entry_date,
                peak_price=max(pos.peak_price, price),
                entry_notional=pos.entry_notional + cost,
                signal_key=pos.signal_key,
                entry_notional_original=pos.entry_notional_original,
                original_entry_price=pos.original_entry_price,
                pyramid_rungs_hit=new_rungs,
            )
        else:
            self.positions[symbol] = SimPosition(
                symbol=symbol,
                qty=qty,
                entry_price=price,
                entry_date=date,
                peak_price=price,
                entry_notional=cost,
                signal_key=signal_key,
                entry_notional_original=cost,
                original_entry_price=price,
                pyramid_rungs_hit=frozenset(),
            )
        return qty

    def fill_sell(self, symbol: str, price: float) -> tuple[float, float, float]:
        """Sell entire position at price. Returns (qty, avg_entry_price, realized_pnl)."""
        if symbol not in self.positions:
            return 0.0, 0.0, 0.0
        pos = self.positions.pop(symbol)
        proceeds = pos.qty * price
        pnl = proceeds - pos.entry_notional
        self.cash += proceeds
        return pos.qty, pos.entry_price, pnl

    def update_peak(self, symbol: str, price: float) -> None:
        pos = self.positions.get(symbol)
        if pos and price > pos.peak_price:
            self.positions[symbol] = SimPosition(
                symbol=pos.symbol,
                qty=pos.qty,
                entry_price=pos.entry_price,
                entry_date=pos.entry_date,
                peak_price=price,
                entry_notional=pos.entry_notional,
                signal_key=pos.signal_key,
                entry_notional_original=pos.entry_notional_original,
                original_entry_price=pos.original_entry_price,
                pyramid_rungs_hit=pos.pyramid_rungs_hit,
            )

    def equity(self, prices: dict[str, float]) -> float:
        mv = sum(
            pos.qty * prices.get(pos.symbol, pos.entry_price)
            for pos in self.positions.values()
        )
        return self.cash + mv
