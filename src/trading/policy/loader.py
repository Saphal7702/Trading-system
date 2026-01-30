from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PolicySnapshot:
    path: str
    meta: dict[str, Any]
    entry_policy: list[dict[str, Any]]
    exit_policy: list[dict[str, Any]]
    best_exits_per_entry: dict[str, list[dict[str, Any]]]

    @property
    def asof(self) -> str | None:
        v = self.meta.get("asof")
        return str(v) if v is not None else None

    def entry_rec(self, entry_key: str) -> dict[str, Any] | None:
        for r in self.entry_policy:
            if r.get("key") == entry_key:
                return r
        return None

    def exit_rec(self, exit_key: str) -> dict[str, Any] | None:
        for r in self.exit_policy:
            if r.get("key") == exit_key:
                return r
        return None

    def best_exits(self, entry_key: str) -> list[dict[str, Any]]:
        return list(self.best_exits_per_entry.get(entry_key, []))


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _pick_latest_policy(paths: list[Path]) -> Path | None:
    """
    Prefer the policy with the newest meta.asof (YYYY-MM-DD),
    fallback to filesystem mtime.
    """
    best: tuple[str, float, Path] | None = None  # (asof, mtime, path)

    for p in paths:
        data = _safe_load_json(p)
        asof = None
        if data and isinstance(data.get("meta"), dict):
            v = data["meta"].get("asof")
            if isinstance(v, str):
                asof = v.strip()

        mtime = p.stat().st_mtime
        key = (asof or "", mtime, p)

        if best is None:
            best = key
            continue

        # compare by asof string first (works for YYYY-MM-DD), then mtime
        if key[0] > best[0] or (key[0] == best[0] and key[1] > best[1]):
            best = key

    return best[2] if best else None


def load_latest_policy(*, policies_dir: str | Path = "policies") -> PolicySnapshot | None:
    """
    Load the latest policy snapshot from ./policies/*.json.
    Returns None if nothing usable exists.
    """
    d = Path(policies_dir)
    if not d.exists() or not d.is_dir():
        return None

    paths = sorted(d.rglob("*.json"))
    if not paths:
        return None

    latest = _pick_latest_policy(paths)
    if latest is None:
        return None

    data = _safe_load_json(latest)
    if not data:
        return None

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    entry_policy = data.get("entry_policy") if isinstance(data.get("entry_policy"), list) else []
    exit_policy = data.get("exit_policy") if isinstance(data.get("exit_policy"), list) else []
    best_exits = data.get("best_exits_per_entry") if isinstance(data.get("best_exits_per_entry"), dict) else {}

    return PolicySnapshot(
        path=str(latest),
        meta=meta,
        entry_policy=entry_policy,
        exit_policy=exit_policy,
        best_exits_per_entry=best_exits,
    )


def format_policy_summary(policy: PolicySnapshot, *, max_entries: int = 3) -> str:
    """
    Short single-line summary for logs.
    """
    asof = policy.asof or "unknown"
    n_entry = len(policy.entry_policy)
    n_exit = len(policy.exit_policy)
    # show up to N BOOST entries
    boosts = [r.get("key") for r in policy.entry_policy if r.get("rec") == "BOOST" and r.get("key")]
    boosts = boosts[:max_entries]
    boosts_s = ",".join(boosts) if boosts else "none"
    return f"policy_asof={asof} entries={n_entry} exits={n_exit} boosts=[{boosts_s}] file={policy.path}"
