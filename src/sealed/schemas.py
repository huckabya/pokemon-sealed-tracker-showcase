"""Arrow schemas for every table. Column names and types are chosen to map
1:1 onto PostgreSQL (int4/int2/text/date/float8/bool/timestamptz)."""
import pyarrow as pa

TS = pa.timestamp("us", tz="UTC")

DIM_GROUP = pa.schema([
    ("group_id", pa.int32()),
    ("category_id", pa.int16()),
    ("name", pa.string()),
    ("abbreviation", pa.string()),
    ("published_on", pa.date32()),
    ("modified_on", TS),
    ("is_supplemental", pa.bool_()),
    ("first_seen", pa.date32()),
    ("last_seen", pa.date32()),
])

DIM_PRODUCT = pa.schema([
    ("product_id", pa.int32()),
    ("group_id", pa.int32()),
    ("name", pa.string()),
    ("clean_name", pa.string()),
    ("url", pa.string()),
    ("image_url", pa.string()),
    ("product_type", pa.string()),
    ("unit_kind", pa.string()),
    ("unit_count", pa.int16()),
    ("retailer_exclusive", pa.string()),
    ("pack_count", pa.int16()),
    ("msrp_usd", pa.float64()),
    ("classification_source", pa.string()),
    ("classifier_version", pa.int16()),
    ("first_seen", pa.date32()),
    ("last_seen", pa.date32()),
])

# One row per (snapshot_date, product_id, sub_type_name). All five TCGplayer
# price fields are kept; nothing is substituted for a missing market price.
FACT_PRICE_DAILY = pa.schema([
    ("snapshot_date", pa.date32()),
    ("product_id", pa.int32()),
    ("sub_type_name", pa.string()),
    ("market_price", pa.float64()),
    ("low_price", pa.float64()),
    ("mid_price", pa.float64()),
    ("high_price", pa.float64()),
    ("direct_low_price", pa.float64()),
    ("source", pa.string()),  # tcgcsv_live | tcgcsv_archive
    ("ingested_at", TS),
])

# Third-party history used only to fill dates before our own collection began.
# Kept in its own table so it is never silently mixed with first-party rows.
FACT_PRICE_SEED = pa.schema([
    ("snapshot_date", pa.date32()),
    ("product_id", pa.int32()),
    ("price", pa.float64()),
    ("price_basis", pa.string()),
    ("source", pa.string()),
    ("source_ref", pa.string()),
])

# Designed now, collected later (decision D-03). Mirrors Pokefin's
# product_sales_history / product_listings_history tables.
FACT_SALES_DAILY = pa.schema([
    ("bucket_date", pa.date32()),
    ("product_id", pa.int32()),
    ("granularity", pa.string()),  # day | week
    ("quantity_sold", pa.int32()),
    ("transaction_count", pa.int32()),
    ("low_sale_price", pa.float64()),
    ("high_sale_price", pa.float64()),
    ("market_price", pa.float64()),
    ("source", pa.string()),
    ("ingested_at", TS),
])

FACT_LISTINGS_DAILY = pa.schema([
    ("snapshot_date", pa.date32()),
    ("product_id", pa.int32()),
    ("active_listings", pa.int32()),
    ("total_quantity_available", pa.int32()),
    ("lowest_listing_price", pa.float64()),
    ("source", pa.string()),
    ("ingested_at", TS),
])
