"""Checks that must pass before a snapshot is written. A failed check stops
the run, so a bad day is a visible red workflow instead of silent bad data."""
from __future__ import annotations

import math
from collections import Counter

import pyarrow as pa

MIN_ROWS = 1000             # category 3 (English); measured: ~3,000 sealed products on 2026-09-26
MAX_DROP_FRACTION = 0.20    # vs the previous snapshot's row count, judged per category
MAX_NULL_MARKET_SHARE = 0.40  # category 3; measured: ~20% of sealed rows have no market price
# Category 85 (Japanese), measured P2-M2 2026-10-21 on the P2-2a classifier: 242 rows, null share 0.219 (D-60).
MIN_ROWS_JA = 100             # floor(242 * 1000 / 2284 / 50) * 50
MAX_NULL_MARKET_SHARE_JA = 0.45  # min(0.80, ceil(2 * 0.219 / 0.05) * 0.05)


def thresholds(category_id: int) -> tuple[int, float]:
    """(min_rows, max_null_market_share) for a TCGCSV category; an unknown category stops the collector."""
    return {3: (MIN_ROWS, MAX_NULL_MARKET_SHARE), 85: (MIN_ROWS_JA, MAX_NULL_MARKET_SHARE_JA)}[category_id]


def check_prices(prices: pa.Table, previous_rows: int | None, min_rows: int | None = None,
                 max_null_market_share: float | None = None) -> list[str]:
    min_rows = MIN_ROWS if min_rows is None else min_rows
    max_null_market_share = MAX_NULL_MARKET_SHARE if max_null_market_share is None else max_null_market_share
    problems: list[str] = []
    n = prices.num_rows
    if n < min_rows:
        problems.append(f"only {n} price rows (< {min_rows})")
    if previous_rows and n < previous_rows * (1 - MAX_DROP_FRACTION):
        problems.append(f"row count fell from {previous_rows} to {n}")
    keys = Counter(zip(prices.column("product_id").to_pylist(),
                       prices.column("sub_type_name").to_pylist()))
    dupes = [k for k, v in keys.items() if v > 1]
    if dupes:
        problems.append(f"{len(dupes)} duplicate (product_id, sub_type_name) keys, e.g. {dupes[:3]}")
    if n:
        null_share = prices.column("market_price").null_count / n
        if null_share > max_null_market_share:
            problems.append(f"{null_share:.0%} of rows have no market price")
    for col in ("market_price", "low_price", "mid_price", "high_price", "direct_low_price"):
        values = [v for v in prices.column(col).to_pylist() if v is not None]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            problems.append(f"non-positive or non-finite values in {col}")
    return problems
