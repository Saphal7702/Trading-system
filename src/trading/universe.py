import csv
from pathlib import Path
from .db import connect

def load_watchlist_csv(csv_path: str) -> int:
    path = Path(csv_path)

    if not path.exists():
        raise FileNotFoundError(f"Watchlist CSV not found: {path}")
    
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if "symbol" not in (reader.fieldnames or []):
            raise ValueError("CSV must have a 'symbol' column header")

        symbols = [] 
        for row in reader:
            sym = (row.get("symbol") or "").strip().upper()
            if sym:
                symbols.append(sym)

    if not symbols:
        return 0
    
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO symbols (symbol, is_active) VALUES (?, 1);", 
            [(s,) for s in symbols],
        )

        conn.executemany(
            "UPDATE symbols SET is_active=1 WHERE symbol=?;",
            [(s,) for s in symbols],
        )

    return len(set(symbols))