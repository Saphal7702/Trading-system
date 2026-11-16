/*********************************************************************************************************************************************************/
A. Pre-execution
    Goal: ensure inputs are valid and the system is allowed to act.

    - Build/load universe config (S&P 500 / filters)
    - Fetch/store daily bars (trading fetch-bars --days 400)
    - Preflight gate (trading preflight)
       - market calendar/time
       - data freshness
       - buying power / cash buffer
       - stuck orders / cooldown / caps

    Output: “safe to run” decision + context (asof date, universe size, bp).

 /*********************************************************************************************************************************************************/

B. Buy pipeline (run-once)
    /************************************************************General Idea*************************************************/
    Goal: generate buy decisions and (optionally) place buy orders safely.

    - Sync broker state: orders / positions / account
    - Build universe snapshot for this run
    - Generate signals (SMA20/50)
    - Plan intents:
        - compliance constraints
        - max positions
        - cash buffer
        - exposure cap per signal_key
        - policy-aware ranking/sizing (still conservative)
    - Execute (only if --execute):
        - submit buys (and any sells already present)
        - execution backstops

    Output: intents + orders + executions (if executed) + updated snapshots.

    /************************************************************Detail Breakdown**************************************************/
    1. Stragey SMA
        - SMA = Simple Moving Average
        - SMA20 = average of last 20 daily closes, SMA20 moves faster (more sensitive), Captures short-term momentum
        - SMA50 = average of last 50 daily closes, SMA50 moves slower (more stable), Represents medium-term trend
        - It is a trend-following entry, not a prediction.
        - Buy logic: SMA20 ↑ crosses SMA50 - “The market is now paying higher prices than it was over the last ~2 months”
            - Recent prices (last ~month) are rising faster than the medium-term trend
            - Momentum has turned positive relative to trend

    1.1 Stragey MRIT
        - Mean Reversion Inside Trend

    2. Raw Signals Generations
        - raw buy signals are computed on the fly from daily OHLCV bars and are not stored in the database.
        - On each run: For each symbol in the universe
            - Load the recent bar window
            - Compute: SMA20, SMA50
            - Compare today vs yesterday


 /*********************************************************************************************************************************************************/

C. Sell pipeline (exits)

    Goal: decide when to exit and execute sells safely.

    - Evaluate exit rules against open lots/positions
    - Emit sell intents (trading exits --emit-intents)
    - Execute sells (your standard execution path, gated)
    - Post-sync: update positions, lots, realized PnL
    
    Output: sell intents + sell orders + realized PnL attribution.

 /*********************************************************************************************************************************************************/

D. Performance, analysis, feedbacks

    Goal: measure reality, learn, and feed it back into policy/strategy.

    - Performance (trading performance)
    - Exposure / concentration (trading exposure)
    - Attribution:
        - exit attribution (attrib-exit --mfe)
        - combo attribution (attrib-combo --mfe)
    - Policy analytics snapshots + recommendations (advisory)
    - Drift/confidence (next) if you choose

    Output: reports + policy snapshots + insights for future changes.


    /*********************************************************************************************************************************************************/

    """
exits_portfolio.py — PORTFOLIO exit philosophy (documentation + env validation).

The PORTFOLIO exit profile is dispatched by exits_advisor._exit_profile_name() for any
position whose entry_signal_key starts with "portfolio_" (set when the position was opened
while TRADING_STRATEGY_MODE=portfolio).  The actual exit evaluation runs through the
standard exits_advisor.evaluate_exit_advice() pipeline; this module only documents the
philosophy and exposes a validation helper.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXIT PHILOSOPHY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NO take-profit ceiling
    Momentum positions can compound large gains.  Take-profit is set to a sentinel
    value (99999%) so it never fires in practice.
    Env: TRADING_EXIT_PROFILE_PORTFOLIO_TAKE_PROFIT_PCT=99999

PORTFOLIO-LEVEL dollar stop  (optional, disabled by default)
    Interpretation: exit if the unrealized dollar LOSS on this position exceeds
    portfolio_stop_pct % of total portfolio equity.

        fire when: abs(unrealized_pnl) / portfolio_equity * 100 >= portfolio_stop_pct

    This gives a portfolio-equity-aware stop that is independent of the position's
    own percentage loss.  For a 10 % position size ($50 on a $500 portfolio):
      • portfolio_stop_pct = 1.5  →  fire at $7.50 loss  →  15 % position loss
      • equivalent to STOP_LOSS_PCT = 15.0 when position sizing is uniform

    When position sizes vary (compound sizing), the portfolio-level stop provides a
    tighter guarantee than the per-position percentage stop.

    Implemented via the ExitRuleConfig.portfolio_stop_dollar_pct field, read from:
      TRADING_EXIT_PROFILE_PORTFOLIO_PORTFOLIO_STOP_PCT  (default: 0 = disabled)

    The per-position hard stop (STOP_LOSS_PCT=15.0) is kept as an independent
    backstop and fires before the portfolio-level check in evaluate_exit_advice().

PER-POSITION hard stop
    TRADING_EXIT_PROFILE_PORTFOLIO_STOP_LOSS_PCT=15.0
    Fires when position return <= -15 %.

TRAILING stop  (let winners run, then protect)
    Activates after peak gain >= 20 %; trail if drawdown from peak >= 10 %.
    TRADING_EXIT_PROFILE_PORTFOLIO_TRAIL_PEAK_PCT=20.0
    TRADING_EXIT_PROFILE_PORTFOLIO_TRAIL_DD_PCT=10.0

BREAK-EVEN floor  (protect a portion of realized gains)
    After peak gain >= 15 %, floor at +3 % return.
    TRADING_EXIT_PROFILE_PORTFOLIO_BREAKEVEN_PEAK_PCT=15.0
    TRADING_EXIT_PROFILE_PORTFOLIO_BREAKEVEN_FLOOR_PCT=3.0

TIME stop  (flat positions only)
    Exit if position held >= 60 days with return < 0 % (dead money).
    TRADING_EXIT_PROFILE_PORTFOLIO_TIME_STOP_DAYS=60
    TRADING_EXIT_PROFILE_PORTFOLIO_TIME_STOP_MIN_RET_PCT=0.0

EARLY FAILURE disabled
    Portfolio positions may need time to develop; early-failure is disabled.
    TRADING_EXIT_PROFILE_PORTFOLIO_EARLY_FAIL_DAYS=999

SMA REVERSAL disabled
    Short-term SMA crossunders are noise for momentum positions; disabled.
    TRADING_EXIT_PROFILE_PORTFOLIO_ENABLE_SMA_REVERSAL=0