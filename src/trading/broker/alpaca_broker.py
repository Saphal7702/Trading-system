from __future__ import annotations

import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

class AlpacaPaperBroker:
    """
    Thin wrapper around Alpaca TradingClient (paper).
    """
    name = "alpaca-paper"
    def __init__(self) -> None:
        key = os.getenv("ALPACA_API_KEY", "").strip()
        secret = os.getenv("ALPACA_API_SECRET", "").strip()
        if not key or not secret:
            raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_API_SECRET in environment")

        # Paper by default (your project assumes paper in early phases)
        self.client = TradingClient(key, secret, paper=True)

    def place_market_order(self, symbol: str, side: str, *, qty: float | None = None, notional: float | None = None):
        side = (side or "").lower().strip()
        if side not in ("buy", "sell"):
            raise ValueError(f"Invalid side={side}")

        if (qty is None and notional is None) or (qty is not None and notional is not None):
            raise ValueError("Provide exactly one of qty or notional")

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            notional=notional,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return self.client.submit_order(req)

    def list_recent_orders(self, status: str = "all", limit: int = 200):
        req = GetOrdersRequest(
            status=QueryOrderStatus(status),
            limit=limit,
            nested=True,
        )
        return self.client.get_orders(req)

    def list_positions(self):
        # Returns list of Position objects
        return self.client.get_all_positions()