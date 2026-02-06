import os

os.environ["TRADING_MAX_POSITIONS"] = "10"
os.environ["TRADING_MAX_EXPOSURE_PER_SIGNAL_KEY"] = "300"
os.environ["TRADING_POLICY_MODE"] = "reduce_only"

from trading.strategy_sma import Signal
from trading.planner import plan_intents

signals = [Signal("AAPL", "buy", "SMA20 crossed above SMA50", strength=0.9)]

intents = plan_intents(signals)
for i in intents:
    print(i.symbol, i.action, i.reason, i.signal_key)
