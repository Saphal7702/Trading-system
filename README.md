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