import sqlite3
from importlib import resources

def apply_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("trading").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)