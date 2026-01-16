import csv
from pathlib import Path
from ..db import connect

def load_universe_csv(universe: str, csv_path: str) -> int:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Universe file not found: {csv_path}")

    symbols: list[str] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "symbol" not in (reader.fieldnames or []):
            raise ValueError("CSV must contain a 'symbol' column")
        for row in reader:
            s = (row.get("symbol") or "").strip().upper()
            if s:
                symbols.append(s)

    if not symbols:
        return 0

    with connect() as conn:
        for s in symbols:
            conn.execute(
                """
                INSERT OR IGNORE INTO universe_membership(universe, symbol)
                VALUES (?, ?);
                """,
                (universe, s),
            )

    return len(set(symbols))
