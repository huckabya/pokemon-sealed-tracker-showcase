"""Build the page-ready "gold" layer.

  python -m sealed.gold --data r2://sealed-data --out gold [--upload gold] [--public]

  gold/manifest.json                          as_of, generated_at, row counts, schema version, public flag
  gold/product_latest.parquet                 one row per sealed product: dims + metrics + relevance
  gold/set_latest.parquet                     one row per set
  gold/watchlist.parquet                      ranked focus products (feeds the Terapeak side repo)
  gold/price_series/group_id=<id>/*.parquet   daily headline price series, one folder per set

--public drops seed history and Terapeak-derived tables (D-25) so the output is safe to publish.
--upload copies the folder into the data store under that prefix (e.g. R2 gold/).
Gold is derived and never committed to git (D-11).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

from sealed import build_stamp
from sealed.metrics import connect
from sealed.store import open_store

GOLD_SCHEMA_VERSION = 2

# Terapeak/attention priority by format: most-traded, most-quoted first.
_TYPE_PRIORITY = "CASE product_type WHEN 'etb' THEN 1 WHEN 'booster_bundle' THEN 2 WHEN 'booster_box' THEN 3 " \
                 "WHEN 'pc_etb' THEN 4 WHEN 'upc' THEN 5 WHEN 'booster_pack' THEN 6 ELSE 9 END"


def build(data_root, out: Path, as_of: dt.date | None = None, public: bool = False,
          upload_prefix: str | None = None) -> dict:
    con = connect(data_root, public=public)
    if as_of is None:
        as_of = con.sql("SELECT max(snapshot_date) FROM v_price_series").fetchone()[0]
    if as_of is None:
        raise SystemExit("no price data yet")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    d = f"DATE '{as_of.isoformat()}'"
    o = out.as_posix()

    con.execute(f"""
      COPY (
        SELECT dp.product_id, dp.group_id, g.name AS set_name, g.abbreviation AS set_code,
               dp.name, dp.url, dp.image_url, dp.product_type, dp.unit_kind, dp.unit_count,
               dp.retailer_exclusive, dp.pack_count, dp.msrp_usd,
               r.tier, coalesce(r.is_focus, false) AS is_focus, coalesce(r.flags, []::VARCHAR[]) AS flags,
               m.* EXCLUDE (product_id, group_id, product_type),
               v.* EXCLUDE (product_id, asof_date),
               pl.pulse_signal, pl.volume_source AS pulse_volume_source,
               tp.* EXCLUDE (product_id, asof_date),
               -- eBay 7-day average sold price vs TCGplayer market price (D-34); private only
               CASE WHEN tp.avg_sold_price_7d_tp IS NOT NULL AND m.last_price > 0
                    THEN tp.avg_sold_price_7d_tp / m.last_price - 1 END AS ebay_vs_tcgplayer_tp
        FROM dim_product dp
        JOIN dim_group g USING (group_id)
        LEFT JOIN product_metrics({d}) m USING (product_id)
        LEFT JOIN product_relevance({d}) r USING (product_id)
        LEFT JOIN product_volume_metrics({d}) v USING (product_id)
        LEFT JOIN product_pulse({d}) pl USING (product_id)
        LEFT JOIN product_volume_terapeak({d}) tp USING (product_id)
        ORDER BY dp.group_id, dp.product_id
      ) TO '{o}/product_latest.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    con.execute(f"""
      COPY (SELECT * FROM set_metrics({d}) ORDER BY published_on DESC)
      TO '{o}/set_latest.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    con.execute(f"""
      COPY (
        SELECT row_number() OVER (
                 ORDER BY (days_since_release < 0), days_since_release, {_TYPE_PRIORITY}, product_id) AS priority,
               product_id, name, set_name, product_type, last_price, flags
        FROM product_relevance({d}) WHERE is_focus
      ) TO '{o}/watchlist.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    con.execute(f"""
      COPY (
        SELECT dp.group_id, s.product_id, s.snapshot_date, s.price, s.low_price, s.price_source
        FROM v_price_series s JOIN dim_product dp USING (product_id)
        WHERE s.snapshot_date <= {d}
        ORDER BY dp.group_id, s.product_id, s.snapshot_date
      ) TO '{o}/price_series' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (group_id))""")

    count = lambda f: con.sql(f"SELECT count(*) FROM read_parquet('{o}/{f}')").fetchone()[0]  # noqa: E731
    manifest = {
        "schema_version": GOLD_SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "public": public,
        **build_stamp(),
        "products": count("product_latest.parquet"),
        "sets": count("set_latest.parquet"),
        "watchlist": count("watchlist.parquet"),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if upload_prefix:
        manifest["uploaded_files"] = open_store(data_root).upload_dir(out, upload_prefix)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("DATA_URI", "data"))
    ap.add_argument("--out", type=Path, default=Path("gold"))
    ap.add_argument("--as-of", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--public", action="store_true", help="exclude seed history and Terapeak-derived tables (D-25)")
    ap.add_argument("--upload", default=None, help="prefix inside the data store to copy gold/ to")
    args = ap.parse_args(argv)
    print(json.dumps(build(args.data, args.out, args.as_of, args.public, args.upload)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
