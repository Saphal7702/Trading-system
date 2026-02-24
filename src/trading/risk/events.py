from __future__ import annotations

import json
from datetime import datetime, timezone
from ..db import connect


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_event(*, env: str, event_type: str, prev_state: str | None, new_state: str | None, metrics: dict, reason: str | None, actor: str) -> None:
    env = (env or "paper").lower()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO risk_events(env, ts, event_type, prev_state, new_state, metrics_json, reason, actor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                env,
                _now_iso(),
                event_type,
                prev_state,
                new_state,
                json.dumps(metrics or {}),
                reason,
                actor,
            ),
        )
