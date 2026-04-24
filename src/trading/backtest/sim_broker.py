from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SimPosition:
    symbol: str
    qty: float
    entry_price: float
    entry_date: str
    peak_price: float
    entry_notional: float


class SimBroker:
    def __init__(self, cash: float) -> None:
        self.cash = float(cash)
        self.positions: dict[str, SimPosition] = {}

    def fill_buy(self, symbol: str, price: float, notional: float, date: str) -> float:
        """Buy at price with given notional. Returns qty filled (0 if insufficient cash)."""
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
            self.positions[symbol] = SimPosition(
                symbol=symbol,
                qty=total_qty,
                entry_price=new_avg,
                entry_date=pos.entry_date,
                peak_price=max(pos.peak_price, price),
                entry_notional=pos.entry_notional + cost,
            )
        else:
            self.positions[symbol] = SimPosition(
                symbol=symbol,
                qty=qty,
                entry_price=price,
                entry_date=date,
                peak_price=price,
                entry_notional=cost,
            )
        return qty

    def fill_sell(self, symbol: str, price: float) -> tuple[float, float, float]:
        """Sell entire position at price. Returns (qty, entry_price, realized_pnl)."""
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
            )

    def equity(self, prices: dict[str, float]) -> float:
        mv = sum(
            pos.qty * prices.get(pos.symbol, pos.entry_price)
            for pos in self.positions.values()
        )
        return self.cash + mv
