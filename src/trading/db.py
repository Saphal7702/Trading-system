import sqlite3
from pathlib import Path
from .schema import apply_schema
from .config import get_settings

def connect() -> sqlite3.Connection:
    settings = get_settings()
    db_path = Path(settings.db_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with connect() as conn:
        apply_schema(conn)