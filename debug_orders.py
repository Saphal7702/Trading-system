import os, sqlite3
from dotenv import load_dotenv

load_dotenv()
db = os.getenv("TRADING_DB_PATH")

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

rows = con.execute("""
SELECT broker_order_id, side, qty, filled_avg_price, filled_at
FROM executions
WHERE symbol = 'AAPL'
ORDER BY filled_at DESC
""").fetchall()

for r in rows:
    print(dict(r))
