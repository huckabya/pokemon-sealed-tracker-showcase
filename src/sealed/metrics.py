"""Open a DuckDB connection with every table and metric macro registered.

    from sealed.metrics import connect
    con = connect("r2://sealed-data")          # or a local folder
    con.sql("SELECT * FROM product_metrics(DATE '2026-09-27') WHERE group_id = 24541").show()

Parquet is read with pyarrow (local disk or R2) and handed to DuckDB as
in-memory Arrow tables, so no DuckDB extension downloads are needed and the
same code path runs everywhere. At ~1M rows/year this fits easily in memory.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa

from sealed import schemas
from sealed.store import Store, open_store

SQL_FILE = Path(__file__).resolve().parents[2] / "sql" / "views.sql"

_DIRS = {
    "fact_price_daily": schemas.FACT_PRICE_DAILY,
    "fact_sales_daily": schemas.FACT_SALES_DAILY,
    "fact_listings_daily": schemas.FACT_LISTINGS_DAILY,
    "fact_price_seed": schemas.FACT_PRICE_SEED,
}
_SINGLE = {
    "dim_group": ("dim_group.parquet", schemas.DIM_GROUP),
    "dim_product": ("dim_product.parquet", schemas.DIM_PRODUCT),
}


# Tables that must never reach a public build (D-25): Terapeak-derived
# sales/listings (eBay data for personal research only). Seed history is
# published with credit (D-39), so it is not listed here.
PRIVATE_TABLES = ("fact_sales_daily", "fact_listings_daily")


def load_tables(root: str | Path | Store, public: bool = False) -> dict[str, pa.Table]:
    st = open_store(root)
    tables = {}
    for name, schema in _DIRS.items():
        rels = [] if (public and name in PRIVATE_TABLES) else st.list_files(name)
        tables[name] = st.read_many(rels, schema)
    for name, (rel, schema) in _SINGLE.items():
        tables[name] = st.read_parquet(rel, schema) if st.exists(rel) else schema.empty_table()
    return tables


def connect(root: str | Path | Store, public: bool = False) -> duckdb.DuckDBPyConnection:
    """public=True leaves out Terapeak-derived tables (required for anything published, D-25);
    the credited seed history stays (D-39)."""
    con = duckdb.connect(":memory:")
    for name, table in load_tables(root, public).items():
        con.register(f"_arrow_{name}", table)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _arrow_{name}")
        con.unregister(f"_arrow_{name}")
    con.execute(SQL_FILE.read_text(encoding="utf-8"))
    return con
