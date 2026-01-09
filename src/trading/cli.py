import argparse
import logging
from .logging_setup import setup_logging
from .config import get_settings
from .db import init_db, connect
from datetime import datetime, timedelta, timezone
from .compliance import can_sell

log = logging.getLogger("trading")

def cmd_healthcheck() -> int:
    s = get_settings()
    log.info("Env: %s", s.env)
    log.info("DB path: %s", s.db_path)

    init_db()

    with connect() as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [r["name"] for r in cur.fetchall()]

    log.info("DB OK. Tables: %s", ", ".join(tables))
    return 0


def cmd_compliance_test() -> int:
    now = datetime.now(timezone.utc)

    opened_recent = (now - timedelta(hours=12)).isoformat()
    d1 = can_sell(opened_recent, now=now)
    log.info("Opened 12h ago -> allowed=%s | %s", d1.allowed, d1.reason)

    opened_old = (now - timedelta(days=2)).isoformat()
    d2 = can_sell(opened_old, now=now)
    log.info("Opened 2d ago  -> allowed=%s | %s", d2.allowed, d2.reason)

    return 0

def main() -> int:
    setup_logging()

    p = argparse.ArgumentParser(prog="trading", description="Personal trading system (Phase 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("healthcheck", help="Verify config + initialize DB schema")
    sub.add_parser("compliance-test")

    args = p.parse_args()

    if args.cmd == "healthcheck":
        return cmd_healthcheck()
    
    if args.cmd == "compliance-test":
        return cmd_compliance_test()

    return 1