from __future__ import annotations

import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from .base import AccountSummary, PositionSnapshot

class AlpacaPaperBroker:
    name = "alpaca"

    def __init__(self) -> None:
        key = os.getenv("ALPACA_API_KEY", "").strip()
        secret = os.getenv("ALPACA_API_SECRET","").strip()
        paper = os.getenv("ALPACA_PAPER","true").strip().lower() in ("1", "true", "yes", "y")

        if not key or not secret:
            raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_API_SECRET in .env")
        
        self.client = TradingClient(api_key=key, secret_key=secret, paper=paper)

    def get_account(self) -> AccountSummary:
        acct = self.client.get_account()

        def f(x):
            try:
                return float(x)
            except Exception:
                return None
        
        return AccountSummary(
            broker=self.name,
            account_id=getattr(acct, "id", None),
            status=getattr(acct, "status", None),
            currency=getattr(acct, "currency", None),
            buying_power=f(getattr(acct, "buying_power", None)),
            equity=f(getattr(acct, "equity", None)),
        )

    def list_positions(self) -> list[PositionSnapshot]:
        positions = self.client.get_all_positions()
        out: list[PositionSnapshot] = []

        def f(x):
            try:
                return float(x)
            except Exception:
                return None
        
        for p in positions:
            out.append(
                PositionSnapshot(
                    symbol=str(getattr(p, "symbol", "")).upper(),
                    qty=float(getattr(p, "qty", 0.0)),
                    avg_entry_price=f(getattr(p, "avg_entry_price", None)),
                )
            )

        return out
    
    def place_market_order(self, symbol: str, side: str, qty: float):
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return self.client.submit_order(req)