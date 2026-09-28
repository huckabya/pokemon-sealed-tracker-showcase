"""Turn one day's raw TCGCSV responses into typed Arrow tables. Pure: no I/O."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from sealed import schemas
from sealed.classify import CLASSIFIER_VERSION, Override, classify


@dataclass
class Snapshot:
    groups: pa.Table
    products: pa.Table
    prices: pa.Table
    excluded: list[dict[str, Any]]


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value[:10]) if value else None


def _ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def build_snapshot(
    groups: list[dict[str, Any]],
    products_by_group: dict[int, list[dict[str, Any]]],
    prices_by_group: dict[int, list[dict[str, Any]]],
    snapshot_date: dt.date,
    ingested_at: dt.datetime,
    overrides: dict[int, Override] | None = None,
    source: str = "tcgcsv_live",
) -> Snapshot:
    group_rows, product_rows, price_rows, excluded = [], [], [], []
    for g in groups:
        gid = int(g["groupId"])
        group_rows.append({
            "group_id": gid,
            "category_id": int(g.get("categoryId", 3)),
            "name": g.get("name"),
            "abbreviation": g.get("abbreviation"),
            "published_on": _date(g.get("publishedOn")),
            "modified_on": _ts(g.get("modifiedOn")),
            "is_supplemental": bool(g.get("isSupplemental", False)),
            "first_seen": snapshot_date,
            "last_seen": snapshot_date,
        })
        sealed_ids: set[int] = set()
        for p in products_by_group.get(gid, []):
            pid = int(p["productId"])
            ext = {e["name"]: e.get("value") for e in p.get("extendedData") or []}
            c = classify(pid, p.get("name", ""), g.get("name", ""), ext, overrides)
            if not c.is_sealed:
                if c.exclusion_reason != "has_collector_number":
                    excluded.append({"product_id": pid, "group_id": gid, "name": p.get("name"),
                                     "reason": c.exclusion_reason})
                continue
            sealed_ids.add(pid)
            product_rows.append({
                "product_id": pid,
                "group_id": gid,
                "name": p.get("name"),
                "clean_name": p.get("cleanName"),
                "url": p.get("url"),
                "image_url": p.get("imageUrl"),
                "product_type": c.product_type,
                "unit_kind": c.unit_kind,
                "unit_count": c.unit_count,
                "retailer_exclusive": c.retailer_exclusive,
                "pack_count": c.pack_count,
                "msrp_usd": c.msrp_usd,
                "classification_source": c.source,
                "classifier_version": CLASSIFIER_VERSION,
                "first_seen": snapshot_date,
                "last_seen": snapshot_date,
            })
        for r in prices_by_group.get(gid, []):
            pid = int(r["productId"])
            if pid not in sealed_ids:
                continue
            price_rows.append({
                "snapshot_date": snapshot_date,
                "product_id": pid,
                "sub_type_name": r.get("subTypeName") or "Normal",
                "market_price": _num(r.get("marketPrice")),
                "low_price": _num(r.get("lowPrice")),
                "mid_price": _num(r.get("midPrice")),
                "high_price": _num(r.get("highPrice")),
                "direct_low_price": _num(r.get("directLowPrice")),
                "source": source,
                "ingested_at": ingested_at,
            })
    return Snapshot(
        groups=pa.Table.from_pylist(group_rows, schema=schemas.DIM_GROUP),
        products=pa.Table.from_pylist(product_rows, schema=schemas.DIM_PRODUCT),
        prices=pa.Table.from_pylist(price_rows, schema=schemas.FACT_PRICE_DAILY),
        excluded=excluded,
    )
