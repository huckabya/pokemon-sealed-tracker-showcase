"""Daily collector: `python -m sealed.collect --data r2://sealed-data`.

`--data` (or $DATA_URI) is a local folder or an R2 bucket URI (D-20).
Exit codes: 0 = wrote a new snapshot or it already existed; 1 = quality
checks failed (nothing written); other non-zero = unexpected error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from sealed import schemas, store
from sealed.classify import load_overrides
from sealed.quality import check_prices
from sealed.snapshot import build_snapshot
from sealed.tcgcsv import TcgcsvClient

PRICE_TABLE = "fact_price_daily"


def run(data_root, client: TcgcsvClient, overrides_path: Path,
        now: dt.datetime | None = None, log=print) -> int:
    st = store.open_store(data_root)
    now = now or dt.datetime.now(dt.timezone.utc)
    snapshot_date = client.last_updated().date()  # UTC date, matches archive naming
    if store.has_daily(st, PRICE_TABLE, snapshot_date):
        log(f"{snapshot_date}: already collected, nothing to do")
        return 0

    groups = client.groups()
    products, prices = {}, {}
    for g in groups:
        gid = int(g["groupId"])
        products[gid] = client.products(gid)
        prices[gid] = client.prices(gid)

    snap = build_snapshot(groups, products, prices, snapshot_date, now, load_overrides(overrides_path))

    prev_day = store.latest_daily(st, PRICE_TABLE)
    prev_rows = st.num_rows(store.daily_rel(PRICE_TABLE, prev_day)) if prev_day else None
    problems = check_prices(snap.prices, prev_rows)
    if problems:
        for p in problems:
            log(f"QUALITY FAIL {snapshot_date}: {p}")
        return 1

    store.write_daily(st, PRICE_TABLE, snapshot_date, snap.prices)
    for name, table, schema, key in (("dim_group", snap.groups, schemas.DIM_GROUP, "group_id"),
                                     ("dim_product", snap.products, schemas.DIM_PRODUCT, "product_id")):
        existing = store.read_table(st, f"{name}.parquet", schema)
        store.write_dim(st, name, store.merge_dim(existing, table, key))
    store.write_excluded_report(st, snap.excluded)
    log(f"{snapshot_date}: {snap.prices.num_rows} price rows, {snap.products.num_rows} sealed products, "
        f"{len(snap.excluded)} excluded -> {st.uri}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=os.environ.get("DATA_URI", "data"))
    ap.add_argument("--overrides", type=Path, default=Path("config/overrides.csv"))
    args = ap.parse_args(argv)
    return run(args.data, TcgcsvClient(), args.overrides)


if __name__ == "__main__":
    sys.exit(main())
