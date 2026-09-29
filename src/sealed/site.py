"""Render the "Sealed Pulse" dashboard: one self-contained HTML page.

  python -m sealed.site --data r2://sealed-data --out site/index.html            # private (includes seed)
  python -m sealed.site --data r2://sealed-data --out site/index.html --public   # showcase-safe (D-25)
  python -m sealed.site ... --fragment    # body-only HTML for a Claude artifact page
  python -m sealed.site ... --public --upload-uri r2://sealed-public   # also write site/index.html to that bucket

The page shows the "most relevant" products (D-22): focus/core/watch tiers,
attention flags, a price chart per product and set-level medians. It carries
metric *values*, never the SQL that computes them (D-24).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from sealed import build_stamp
from sealed.metrics import connect
from sealed.store import open_store

TEMPLATE = Path(__file__).with_name("site_template.html")
EPOCH = dt.date(2024, 1, 1)  # series dates are sent as day offsets from here


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


def payload(data_root, as_of: dt.date | None = None, public: bool = False) -> dict:
    con = connect(data_root, public=public)
    if as_of is None:
        as_of = con.sql("SELECT max(snapshot_date) FROM v_price_series").fetchone()[0]
    d = f"DATE '{as_of.isoformat()}'"

    rel = con.sql(f"""
      SELECT r.product_id, r.name, r.set_name, r.group_id, r.product_type, r.tier, r.is_focus, r.flags,
             r.last_price, r.ret_30d, r.ret_90d, r.drawdown_from_peak, r.days_since_peak,
             r.trend_90d_per_30d, r.trend_90d_r2, r.zscore_30d, r.pack_premium, r.msrp_premium,
             r.days_since_release, dp.url, dp.pack_count
      FROM product_relevance({d}) r JOIN dim_product dp USING (product_id)
    """).fetchall()
    products = []
    for row in rel:
        (pid, name, set_name, gid, ptype, tier, focus, flags, price, r30, r90, dd, dsp,
         trend, r2, z, pack, msrp, dsr, url, packs) = row
        products.append({
            "id": pid, "name": name, "set": set_name or "", "gid": gid, "type": ptype, "tier": tier,
            "focus": bool(focus), "flags": list(flags or []), "price": _r(price, 2), "r30": _r(r30),
            "r90": _r(r90), "dd": _r(dd), "dsp": dsp, "trend": _r(trend), "r2": _r(r2, 3), "z": _r(z, 2),
            "pack": _r(pack), "msrp": _r(msrp), "dsr": dsr, "url": url, "packs": packs,
        })

    ids = ",".join(str(p["id"]) for p in products) or "NULL"
    if not public:
        _add_ebay(con, d, ids, {p["id"]: p for p in products})
    series: dict[str, list[list[float]]] = {}
    for pid, day, price in con.sql(f"""
        SELECT product_id, snapshot_date, price FROM v_price_series
        WHERE product_id IN ({ids}) AND snapshot_date <= {d}
        ORDER BY product_id, snapshot_date""").fetchall():
        series.setdefault(str(pid), []).append([(day - EPOCH).days, round(price, 2)])

    sets = [{"name": n, "published": p.isoformat() if p else None, "n": k, "med30": _r(m30), "med90": _r(m90),
             "cons": _r(c, 3), "dd": _r(dd)}
            for n, p, k, m30, m90, c, dd in con.sql(f"""
        SELECT s.set_name, s.published_on, s.n_products, s.median_ret_30d, s.median_ret_90d,
               s.consistency_90d, s.median_drawdown_from_peak
        FROM set_metrics({d}) s JOIN v_product_line_sets ls USING (group_id)
        WHERE {d} - s.published_on BETWEEN 0 AND 730 AND s.n_products >= 3
        ORDER BY s.published_on DESC""").fetchall()]

    sources = [s for (s,) in con.sql("SELECT DISTINCT price_source FROM v_price_series").fetchall()]
    return {
        "meta": {"as_of": as_of.isoformat(), "epoch": EPOCH.isoformat(), "public": public,
                 "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                 "sources": sorted(sources), **build_stamp()},
        "products": products, "series": series, "sets": sets,
    }


def _add_ebay(con, d: str, ids: str, by_id: dict) -> None:
    """The eBay (Terapeak) panel: private research data, never queried for a public build (D-25, D-34)."""
    rows = con.sql(f"""
      SELECT t.product_id, t.volume_newest_day_tp, t.units_sold_7d_tp, t.units_sold_28d_tp, t.avg_sold_price_7d_tp,
             CASE WHEN t.avg_sold_price_7d_tp IS NOT NULL AND m.last_price > 0
                  THEN t.avg_sold_price_7d_tp / m.last_price - 1 END AS gap,
             t.active_listings_tp, t.active_listings_capped_tp, t.days_of_supply_tp
      FROM product_volume_terapeak({d}) t JOIN product_metrics({d}) m USING (product_id)
      WHERE t.product_id IN ({ids})
    """).fetchall()
    for pid, newest, u7, u28, avg7, gap, listings, capped, dos in rows:
        if all(v is None for v in (u7, u28, avg7, listings, dos)):
            continue
        by_id[pid]["ebay"] = {
            "newest": newest.isoformat() if newest else None, "u7": u7, "u28": u28, "avg7": _r(avg7, 2),
            "gap": _r(gap), "listings": listings, "capped": bool(capped), "dos": _r(dos, 1),
        }


def render(data: dict, fragment: bool = False) -> str:
    body = TEMPLATE.read_text(encoding="utf-8")
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    body = body.replace("/*__SEALED_DATA__*/null", blob)
    if fragment:
        return body
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "</head>\n<body>\n" + body + "\n</body>\n</html>\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("DATA_URI", "data"))
    ap.add_argument("--out", type=Path, default=Path("site/index.html"))
    ap.add_argument("--as-of", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--public", action="store_true", help="exclude Terapeak-derived tables (D-25)")
    ap.add_argument("--fragment", action="store_true", help="body-only HTML (for a Claude artifact)")
    ap.add_argument("--upload-uri", default=None, help="data store to copy the page to as site/index.html")
    args = ap.parse_args(argv)
    if args.upload_uri and not args.public:
        ap.error("--upload-uri publishes the page, so it requires --public (D-25)")
    data = payload(args.data, args.as_of, args.public)
    html = render(data, args.fragment)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    if args.upload_uri:
        open_store(args.upload_uri).write_bytes("site/index.html", html.encode("utf-8"))
    print(f"{args.out}: {len(data['products'])} products, {len(data['sets'])} sets, as of {data['meta']['as_of']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
