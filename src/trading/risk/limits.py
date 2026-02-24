from __future__ import annotations

from ..db import connect
from .state import seed_risk_defaults


def get_limits(env: str) -> dict:
    env = (env or "paper").lower()
    seed_risk_defaults(env)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM risk_limits WHERE env=?;",
            (env,),
        ).fetchone()
    return dict(row) if row else {}
