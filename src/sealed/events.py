"""The event table in config/events.csv (contract C4) and its validating loader; published with site.py (R-11)."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

from sealed import config_csv

EVENT_KINDS = ("reprint", "restock", "pc_drop", "holiday", "other")
EVENT_SCOPES = ("product", "set")
EVENT_STATUSES = ("official", "retailer", "community_rumor")
EVENT_COLUMNS = ["scope", "id", "date", "kind", "status", "source", "note"]
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _bad(row: int, column: str, value: str, expected: str) -> ValueError:
    return ValueError(f"events row {row}, column {column}: {value!r} is not {expected}")


def _parse_row(row: int, raw: dict) -> dict:
    scope, kind = raw["scope"].strip(), raw["kind"].strip()
    status, source, note = raw["status"].strip(), raw["source"].strip(), raw["note"].strip()
    if scope not in EVENT_SCOPES:
        raise _bad(row, "scope", scope, f"one of {EVENT_SCOPES}")
    if not re.fullmatch(r"[0-9]+", raw["id"].strip()):
        raise _bad(row, "id", raw["id"], "a whole-number product_id or group_id")
    if kind not in EVENT_KINDS:
        raise _bad(row, "kind", kind, f"one of {EVENT_KINDS}")
    text = raw["date"].strip()
    try:
        if not _ISO_DATE.fullmatch(text):
            raise ValueError
        date = dt.date.fromisoformat(text)
    except ValueError:
        raise _bad(row, "date", text, "an ISO YYYY-MM-DD date") from None
    if status not in EVENT_STATUSES:
        raise _bad(row, "status", status, f"one of {EVENT_STATUSES}")
    if not source and status != "community_rumor":
        raise _bad(row, "source", source, "a source (URL or citation); only community_rumor may omit it")
    if "terapeak" in source.lower():
        raise _bad(row, "source", source, "free of Terapeak references (D-25, D-46)")
    if "terapeak" in note.lower():
        raise _bad(row, "note", note, "free of Terapeak references (D-25, D-46)")
    return {"scope": scope, "id": int(raw["id"]), "date": date, "kind": kind, "status": status,
            "source": source, "note": note}


def load_events(path) -> list[dict]:
    """Events in file order; ValueError names the row (1-based among the data rows) and the
    column of the first bad value, or a missing header column."""
    try:
        raw_rows = config_csv.rows(path, EVENT_COLUMNS)
    except config_csv.ConfigCsvError as err:
        raise ValueError(err.as_loader_text("events", EVENT_COLUMNS)) from None
    events = []
    row_of_key: dict[str, int] = {}
    for n, raw in enumerate(raw_rows, 1):
        event = _parse_row(n, raw)
        key = event_key(event)
        if key in row_of_key:
            raise ValueError(f"events rows {row_of_key[key]} and {n} share one event key (scope, id, date, kind, source)")
        row_of_key[key] = n
        events.append(event)
    return events


def event_key(event: dict) -> str:
    """Stable identity of an event across rebuilds: scope, id, date, kind and source."""
    parts = [event["scope"], str(event["id"]), event["date"].isoformat(), event["kind"], event["source"]]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _sort_key(event: dict) -> tuple:
    return (event["date"], EVENT_SCOPES.index(event["scope"]), event["id"], event["kind"],
            event["status"], event["source"], event["note"])


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _field(text: str) -> str:
    return _quote(text) if any(c in text for c in ',"\n\r') else text


def dump_events(events, path) -> None:
    """Canonical file: UTF-8 without BOM, LF endings, sorted rows, `note` always quoted."""
    lines = [",".join(EVENT_COLUMNS)]
    for e in sorted(events, key=_sort_key):
        fields = [e["scope"], str(e["id"]), e["date"].isoformat(), e["kind"], e["status"], _field(e["source"])]
        lines.append(",".join(fields + [_quote(e["note"])]))
    Path(path).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
