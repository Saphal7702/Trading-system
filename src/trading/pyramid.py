from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("trading")


@dataclass
class PyramidConfig:
    ladder: list[float]  # gain % thresholds, e.g. [20.0, 50.0, 100.0]
    sizes: list[float]   # add size as fraction of original notional, e.g. [0.5, 0.33, 0.17]
    max_mult: float      # max total notional as multiple of original, e.g. 2.0


def is_pyramid_enabled() -> bool:
    v = os.environ.get("TRADING_PORTFOLIO_PYRAMID_ENABLED", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def load_pyramid_config() -> Optional[PyramidConfig]:
    """Load and validate pyramid config from env vars. Returns None if disabled or invalid."""
    if not is_pyramid_enabled():
        return None
    try:
        ladder = [
            float(x)
            for x in os.environ.get("TRADING_PORTFOLIO_PYRAMID_LADDER", "20,50,100").split(",")
        ]
        sizes = [
            float(x)
            for x in os.environ.get("TRADING_PORTFOLIO_PYRAMID_SIZES", "0.5,0.33,0.17").split(",")
        ]
        max_mult = float(os.environ.get("TRADING_PORTFOLIO_PYRAMID_MAX_MULT", "2.0"))
    except (ValueError, TypeError) as e:
        log.error("PYRAMID config parse error: %s — pyramiding disabled", e)
        return None

    if not ladder:
        log.error("PYRAMID config: LADDER is empty — pyramiding disabled")
        return None
    if len(ladder) != len(sizes):
        log.error("PYRAMID config: LADDER and SIZES must have same length — pyramiding disabled")
        return None
    for i in range(1, len(ladder)):
        if ladder[i] <= ladder[i - 1]:
            log.error("PYRAMID config: LADDER must be strictly ascending — pyramiding disabled")
            return None
    if any(s <= 0 for s in sizes):
        log.error("PYRAMID config: all SIZES must be positive — pyramiding disabled")
        return None
    if max_mult <= 1.0:
        log.error("PYRAMID config: MAX_MULT must be > 1.0 — pyramiding disabled")
        return None

    total_add = sum(sizes)
    if 1.0 + total_add > max_mult + 0.01:
        log.warning(
            "PYRAMID config: sum(sizes)=%.2f + 1.0 = %.2f > max_mult=%.2f — some rungs may be capped",
            total_add, 1.0 + total_add, max_mult,
        )

    return PyramidConfig(ladder=ladder, sizes=sizes, max_mult=max_mult)


def check_pyramid_add(
    *,
    original_entry_price: float,
    entry_notional_original: float,
    current_notional: float,
    current_price: float,
    rungs_hit: set[int],
    config: PyramidConfig,
) -> Optional[tuple[int, float]]:
    """Return (rung_index, add_notional) for the lowest eligible rung not yet hit, or None.

    Mirrors backtest _portfolio_pyramid_check() exactly, including the epsilon guard
    for float rounding at threshold boundaries.
    """
    if original_entry_price <= 0 or entry_notional_original <= 0:
        return None

    gain_pct = (current_price / original_entry_price - 1.0) * 100.0

    for i, threshold in enumerate(config.ladder):
        if i in rungs_hit:
            continue
        if gain_pct >= threshold - 1e-9:
            add_notional = entry_notional_original * config.sizes[i]
            if current_notional + add_notional > entry_notional_original * config.max_mult:
                continue
            return (i, add_notional)

    return None
