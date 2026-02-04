## High-Level Architecture

This system is a personal, safety-first, end-of-day trading workflow. It prioritizes correctness, auditability, and conservative execution. The CLI entrypoint (`trading`) orchestrates a single daily run via `run_once`, which handles gating, planning, optional execution, and accounting.

## Data Flow (Run Once)

1. **Initialize + Lock**
   - Initialize SQLite and acquire a cron-safe lock.
   - Create a `runs` row for auditability.

2. **Resolve `asof` + Staleness Gate**
   - Resolve the latest `asof` date from stored bars.
   - Skip if bars are stale or not latest (env-gated).

3. **Policy Load (Advisory Only)**
   - Load the latest policy snapshot from `policies/`.
   - Persist policy metadata onto `runs` when available.

4. **Broker + Calendar Gate**
   - Verify broker connectivity and account state.
   - Gate on market calendar and (optionally) after-close timing.

5. **Pre-Sync Broker State**
   - Sync orders and positions into local DB.

6. **Build Universe Snapshot**
   - Build a daily universe (top N by liquidity, etc.).

7. **Signals**
   - Generate SMA20/SMA50 signals at `asof`.

8. **Plan Intents**
   - Enforce compliance (min-hold before sell).
   - Apply budget and max-position constraints.
   - Overlay policy advice (sizing, annotations).
   - Persist intents to DB.

9. **Execute (Optional)**
   - Execution only occurs when explicitly requested.

10. **Post-Sync Broker State**
    - Sync orders and positions again after execution.

11. **Accounting**
    - Apply executions to lot accounting (realized PnL).
    - Record daily account snapshot for equity curve.

12. **Finalize**
    - Persist run summary and status, release lock.

## Core Modules

- `src/trading/cli.py`: CLI entrypoint for all commands
- `src/trading/runloop.py`: `run_once` orchestration and gating
- `src/trading/strategy_sma.py`: SMA signal generation
- `src/trading/planner.py`: compliance + planning + policy overlay
- `src/trading/execution.py`: order submission and tracking
- `src/trading/pnl/`: lots and daily snapshots
- `src/trading/policy/`: policy analytics loader and helpers
- `src/trading/broker/`: Alpaca integrations + sync logic

## Data Stores

- SQLite (single source of truth for runs, intents, orders, executions, positions, lots, and snapshots)
- Policy snapshots in `policies/*.json`
- Universe files in `data/`

## Mermaid Flow (High Level)

```mermaid
flowchart TD
  A[CLI: trading] --> B[run_once]
  B --> C[Init DB + Lock]
  C --> D[Resolve asof + Staleness Gate]
  D --> E[Load Policy Snapshot]
  E --> F[Broker Check + Calendar Gate]
  F --> G[Sync Orders/Positions (Pre)]
  G --> H[Build Universe]
  H --> I[Generate SMA Signals]
  I --> J[Plan Intents + Compliance + Policy Overlay]
  J --> K{Execute Requested?}
  K -->|No| L[Skip Execution]
  K -->|Yes| M[Execute Orders]
  L --> N[Sync Orders/Positions (Post)]
  M --> N
  N --> O[Apply Lots + Account Snapshot]
  O --> P[Finalize Run]
```
