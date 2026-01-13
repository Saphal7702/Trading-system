import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

db = os.getenv("TRADING_DB_PATH")
print("DB:", db)

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

rows = con.execute(
    """
    SELECT id, symbol, side, status, reason, requested_at, broker_order_id
    FROM orders
    ORDER BY id DESC
    LIMIT 5
    """
).fetchall()

for r in rows:
    print(dict(r))
