1) Pre-execution
    Goal: ensure inputs are valid and the system is allowed to act.

    - Build/load universe config (S&P 500 / filters)
    - Fetch/store daily bars (trading fetch-bars --days 400)
    - Preflight gate (trading preflight)
       - market calendar/time
       - data freshness
       - buying power / cash buffer
       - stuck orders / cooldown / caps

    Output: “safe to run” decision + context (asof date, universe size, bp).

2) Buy pipeline (run-once)

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

3) Sell pipeline (exits)

    Goal: decide when to exit and execute sells safely.

    - Evaluate exit rules against open lots/positions
    - Emit sell intents (trading exits --emit-intents)
    - Execute sells (your standard execution path, gated)
    - Post-sync: update positions, lots, realized PnL
    
    Output: sell intents + sell orders + realized PnL attribution.

4) Performance, analysis, feedback

    Goal: measure reality, learn, and feed it back into policy/strategy.

    - Performance (trading performance)
    - Exposure / concentration (trading exposure)
    - Attribution:
        - exit attribution (attrib-exit --mfe)
        - combo attribution (attrib-combo --mfe)
    - Policy analytics snapshots + recommendations (advisory)
    - Drift/confidence (next) if you choose

    Output: reports + policy snapshots + insights for future changes.