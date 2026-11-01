"""Net proceeds for one sale from the fee table (roadmap C5, C6; decision D-52).

`config/fees.csv` has one row per fee component. Columns: venue, account_type,
effective_from, component, pct, fixed, per (item or order), base, band_from,
band_to, band_rule, cap, sales_4wk_from, sales_4wk_to, source_url, checked_on,
notes. Rows with account_type `mine` are the seller's own settings: a blank one means
"use the published fee, or none". When a fee changes, repeat every row of that
component with the later effective_from: the newest dated group replaces the
whole component, and load_fees refuses a group that drops a (per, base) pair of
the group before it (D-52). End a sub-fee with a pct 0 / fixed 0 row; never edit
old rows. mine rows and a table passed as table= are not checked. Nothing is
read on import.
"""

from datetime import date
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path

from sealed import config_csv

FEES_CSV = Path(__file__).resolve().parents[2] / "config" / "fees.csv"
COLUMNS = ("venue", "account_type", "effective_from", "component", "pct", "fixed", "per", "base", "band_from",
           "band_to", "band_rule", "cap", "sales_4wk_from", "sales_4wk_to", "source_url", "checked_on", "notes")
VENUES = ("tcgplayer", "ebay", "whatnot")
DEFAULT_ACCOUNT = {"tcgplayer": "marketplace_level_1_4", "ebay": "no_store", "whatnot": "standard"}
KEYS = ("gross", "fees", "shipping", "lot_discount", "net")
ROUNDING = {"tcgplayer": ROUND_HALF_EVEN, "ebay": ROUND_HALF_UP, "whatnot": ROUND_HALF_UP}

NUMBERS = ("pct", "fixed", "band_from", "band_to", "cap")
BASES = {"", "item", "shipping", "sale_total", "order_total"}
CENT = Decimal("0.01")


class NoFeesInForce(ValueError):
    pass


def load_fees(path=FEES_CSV):
    try:
        raw_rows = config_csv.rows(path, COLUMNS)
    except config_csv.ConfigCsvError as err:
        raise ValueError(err.as_loader_text("fees", COLUMNS)) from None
    rows = [_row(n, raw) for n, raw in enumerate(raw_rows, 1)]
    _check_dated_groups(rows)
    return rows


def _row(n, raw):
    def bad(col, reason):
        return ValueError(f"fees row {n}, column {col}: {reason}")

    row = dict(raw)
    if row["venue"] not in VENUES:
        raise bad("venue", f"{row['venue']!r} is not one of {VENUES}")
    for col in ("account_type", "component"):
        if not row[col]:
            raise bad(col, "is blank")
    try:
        row["effective_from"] = date.fromisoformat(row["effective_from"]) if row["effective_from"] else None
    except ValueError:
        raise bad("effective_from", f"{row['effective_from']!r} is not a YYYY-MM-DD date") from None
    for col in NUMBERS:
        try:
            row[col] = Decimal(row[col]) if row[col] else None
        except ArithmeticError:
            raise bad(col, f"{row[col]!r} is not a number") from None
        if row[col] is not None and not row[col].is_finite():
            raise bad(col, f"{row[col]} is not finite")
    if row["per"] not in {"item", "order"}:
        raise bad("per", f"{row['per']!r} is not item or order")
    if row["base"] not in BASES or (not row["base"] and row["pct"] is not None):
        raise bad("base", f"{row['base']!r} is not a usable base")
    if row["band_rule"] not in {"", "portion", "whole"}:
        raise bad("band_rule", f"{row['band_rule']!r} is not portion or whole")
    if row["account_type"] != "mine" and not row["source_url"]:
        raise bad("source_url", "a published row needs a source")
    return row


def _check_dated_groups(rows):
    components = {}
    for n, row in enumerate(rows, 1):
        if row["account_type"] != "mine":
            key = (row["venue"], row["account_type"], row["component"])
            components.setdefault(key, {}).setdefault(row["effective_from"], []).append((n, row))
    for (venue, account_type, component), dated in components.items():
        dates = sorted(dated, key=lambda d: (d is not None, d or date.min))
        for earlier, later in zip(dates, dates[1:]):
            dropped = _pairs(dated[earlier]) - _pairs(dated[later])
            if dropped:
                per, base = min(dropped)
                raise ValueError(
                    f"fees row {dated[later][0][0]}, column effective_from: {venue} {account_type} {component} "
                    f"rows dated {later.isoformat()} drop per={per} base={base} from the rows dated "
                    f"{earlier.isoformat() if earlier else 'blank'}; repeat every row of the component with the "
                    "new date, or end that fee with a row of pct 0 and fixed 0")


def _pairs(numbered_rows):
    return {(row["per"], row["base"]) for _, row in numbered_rows}


def _in_force(table, venue, account_type, as_of):
    kept = [r for r in table
            if r["venue"] == venue
            and r["account_type"] in (account_type, "all", "mine")
            and (r["effective_from"] is None or r["effective_from"] <= as_of)
            and not (r["account_type"] == "mine" and r["pct"] is None and r["fixed"] is None)]
    latest = {}
    for r in kept:
        key = (r["account_type"], r["component"])
        latest[key] = max(latest.get(key, date.min), r["effective_from"] or date.min)
    rows = [r for r in kept if (r["effective_from"] or date.min) == latest[(r["account_type"], r["component"])]]
    if not any(r["account_type"] == account_type for r in rows):
        raise NoFeesInForce(f"no {venue} {account_type} fees in force on {as_of}")
    mine = {r["component"] for r in rows if r["account_type"] == "mine"}
    return [r for r in rows if r["account_type"] == "mine" or r["component"] not in mine]


def _amount(row, base):
    pct, fixed = row["pct"] or 0, row["fixed"] or 0
    low, high = row["band_from"], row["band_to"]
    if row["band_rule"] == "portion":
        top = base if high is None else min(base, high)
        amount = pct * max(0, top - (low or 0)) + fixed
    elif row["band_rule"] == "whole":
        rounded = base.quantize(CENT, rounding=ROUND_HALF_UP)
        if (low is not None and rounded < low) or (high is not None and rounded > high):
            return Decimal(0)
        amount = pct * base + fixed
    else:
        amount = pct * base + fixed
    return amount if row["cap"] is None else min(amount, row["cap"])


def net_proceeds(price, qty, venue, as_of, lot=False, *, account_type=None, buyer_shipping=0, postage=None,
                 tax_rate=0, table=None):
    if venue not in VENUES:
        raise ValueError(f"venue {venue!r} is not one of {VENUES}")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        raise ValueError(f"qty must be an int of at least 1, got {qty!r}")
    if account_type in ("all", "mine"):
        raise ValueError(f"account_type {account_type!r} is not a seller account type")
    account_type = account_type or DEFAULT_ACCOUNT[venue]
    rows = _in_force(load_fees() if table is None else table, venue, account_type, as_of)

    rounding = ROUNDING[venue]

    def cents(x):
        return x.quantize(CENT, rounding=rounding)

    lot_rows = [r for r in rows if r["component"] == "my_lot_discount"]
    ship_rows = [r for r in rows if r["component"] == "my_shipping_cost"]
    fee_rows = [r for r in rows if r["component"] not in ("my_lot_discount", "my_shipping_cost")]

    items = Decimal(str(price)) * qty
    lot_amount = sum((_amount(r, items) for r in lot_rows), Decimal(0)) if lot else Decimal(0)
    sold = items - lot_amount
    ship = Decimal(str(buyer_shipping))
    taxed = (sold + ship) * (1 + Decimal(str(tax_rate)))
    order_base = {"item": sold, "shipping": ship, "sale_total": taxed, "order_total": taxed, "": Decimal(0)}
    item_base = {k: v / qty for k, v in order_base.items()}

    fees = cents(sum((_amount(r, item_base[r["base"]]) * qty if r["per"] == "item"
                      else _amount(r, order_base[r["base"]]) for r in fee_rows), Decimal(0)))
    lot_discount = cents(lot_amount)
    shipping = _shipping(ship_rows, postage, cents)
    gross = cents(items + ship)
    net = gross - fees - shipping - lot_discount
    return {k: float(v) for k, v in zip(KEYS, (gross, fees, shipping, lot_discount, net))}


def _shipping(ship_rows, postage, cents):
    if postage is not None:
        return cents(Decimal(str(postage)))
    if len(ship_rows) > 1:
        raise ValueError("more than one my_shipping_cost row in force; pass postage=")
    return cents(_amount(ship_rows[0], Decimal(0))) if ship_rows else Decimal(0)
