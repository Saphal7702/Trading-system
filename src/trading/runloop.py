from dataclasses import dataclass
from .db import connect

@dataclass
class RunResult:
    run_id: int

def start_run(notes: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute("INSERT INTO runs (notes) VALUES (?);",(notes, ))
        return int(cur.lastrowid)
    
def finish_run(run_id: int, status:str = "success") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = datetime('now'), status=? WHERE ID=?;",
            (status, run_id),
        )

def run_once(notes: str | None=None) -> RunResult:
    run_id = start_run(notes=notes)
    
    try:
        finish_run(run_id, status="success")
        return RunResult(run_id=run_id)
    except Exception:
        finish_run(run_id, status="failed")
        raise