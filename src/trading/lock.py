from __future__ import annotations

import os
import json
import time
import errno
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("trading")


@dataclass(frozen=True)
class LockInfo:
    path: str
    owner_pid: int | None
    created_at_utc: str | None
    age_seconds: int | None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_lock(path: Path) -> dict | None:
    try:
        txt = path.read_text(encoding="utf-8")
        return json.loads(txt)
    except Exception:
        return None


def acquire_lock(lock_name: str = "trading.lock") -> tuple[bool, str]:
    """
    Create an exclusive lock file atomically.
    - Uses TRADING_LOCK_PATH if set, else uses "<db_dir>/<lock_name>" if db_path is available,
      else uses cwd.
    - TTL via TRADING_LOCK_TTL_SECONDS (default 6 hours).
    Returns (acquired, lock_path).
    """
    ttl = int(float(os.getenv("TRADING_LOCK_TTL_SECONDS", "21600")))  # 6h default
    explicit = os.getenv("TRADING_LOCK_PATH")

    if explicit:
        lock_path = Path(explicit).expanduser().resolve()
    else:
        # Try to anchor lock next to DB file if you use TRADING_DB_PATH/DB_PATH style envs.
        db_path = os.getenv("TRADING_DB_PATH") or os.getenv("DB_PATH")
        if db_path:
            lock_path = Path(db_path).expanduser().resolve().with_suffix(".lock")
        else:
            lock_path = Path.cwd() / lock_name

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "pid": os.getpid(),
        "created_at_utc": _now_utc_iso(),
    }
    payload_txt = json.dumps(payload)

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
        try:
            os.write(fd, payload_txt.encode("utf-8"))
        finally:
            os.close(fd)
        return (True, str(lock_path))
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

        # Existing lock: check TTL
        st = None
        try:
            st = lock_path.stat()
        except Exception:
            return (False, str(lock_path))

        age = int(time.time() - st.st_mtime)
        if age > ttl:
            log.warning("Stale lock detected (age=%ss > ttl=%ss). Reclaiming: %s", age, ttl, lock_path)
            try:
                lock_path.unlink(missing_ok=True)  # py3.8+; on older, wrap in try
            except Exception:
                pass

            # One retry
            try:
                fd = os.open(str(lock_path), flags)
                try:
                    os.write(fd, payload_txt.encode("utf-8"))
                finally:
                    os.close(fd)
                return (True, str(lock_path))
            except OSError:
                return (False, str(lock_path))

        return (False, str(lock_path))


def read_lock_info(lock_path: str) -> LockInfo:
    p = Path(lock_path)
    if not p.exists():
        return LockInfo(path=lock_path, owner_pid=None, created_at_utc=None, age_seconds=None)

    data = _read_lock(p) or {}
    pid = data.get("pid")
    created = data.get("created_at_utc")

    try:
        age = int(time.time() - p.stat().st_mtime)
    except Exception:
        age = None

    return LockInfo(path=lock_path, owner_pid=pid, created_at_utc=created, age_seconds=age)


def release_lock(lock_path: str | None) -> None:
    if not lock_path:
        return
    try:
        Path(lock_path).unlink(missing_ok=True)
    except Exception:
        pass
