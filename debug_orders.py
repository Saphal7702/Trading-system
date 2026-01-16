import os, sqlite3
from dotenv import load_dotenv

load_dotenv()
db = os.getenv("TRADING_DB_PATH")

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

rows = con.execute("""
SELECT symbol, tradable, fractionable, status, exchange FROM assets_cache WHERE symbol='LLY';
""").fetchall()

for r in rows:
    print(dict(r))
