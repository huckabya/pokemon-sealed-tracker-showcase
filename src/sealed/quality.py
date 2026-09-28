"""Checks that must pass before a snapshot is written. A failed check stops
the run, so a bad day is a visible red workflow instead of silent bad data."""
from __future__ import annotations

from collections import Counter

import pyarrow as pa

MIN_ROWS = 1000             # measured: ~3,000 sealed products on 2026-09-26
MAX_DROP_FRACTION = 0.20    # vs the previous snapshot's row count
MAX_NULL_MARKET_SHARE = 0.40  # measured: ~20% of sealed rows have no market price


def check_prices(prices: pa.Table, previous_rows: int | None) -> list[str]:
    problems: list[str] = []
    n = prices.num_rows
    if n < MIN_ROWS:
        problems.append(f"only {n} price rows (< {MIN_ROWS})")
    if previous_rows and n < previous_rows * (1 - MAX_DROP_FRACTION):
        problems.append(f"row count fell from {previous_rows} to {n}")
    keys = Counter(zip(prices.column("product_id").to_pylist(),
                       prices.column("sub_type_name").to_pylist()))
    dupes = [k for k, v in keys.items() if v > 1]
    if dupes:
        problems.append(f"{len(dupes)} duplicate (product_id, sub_type_name) keys, e.g. {dupes[:3]}")
    if n:
        null_share = prices.column("market_price").null_count / n
        if null_share > MAX_NULL_MARKET_SHARE:
            problems.append(f"{null_share:.0%} of rows have no market price")
    for col in ("market_price", "low_price", "mid_price", "high_price", "direct_low_price"):
        values = [v for v in prices.column(col).to_pylist() if v is not None]
        if any(v <= 0 for v in values):
            problems.append(f"non-positive values in {col}")
    return problems
